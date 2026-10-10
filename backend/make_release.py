"""Validate a signed APK with Android build-tools and atomically stage a monotonic release."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

# Match UpdateDownload.MAX_APK_BYTES: bundled offline speech makes the APK >300 MiB.
MAX_APK = 1024 * 1024 * 1024
PACKAGE = 'local.voice'


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(65536):
            value.update(block)
    return value.hexdigest()


def command(argv):
    result = subprocess.run([str(value) for value in argv], capture_output=True, text=True,
                            timeout=120, check=True, shell=False)
    return result.stdout


def inspect_apk(path, build_tools, certificate):
    # All checks operate on the private snapshot, never the mutable build output.
    output = command([build_tools / 'apksigner', 'verify', '--print-certs', path])
    signers = re.findall(r'^Signer #\d+ certificate SHA-256 digest: ([a-fA-F0-9]{64})$', output, re.M)
    if [value.lower() for value in signers] != [certificate]:
        raise ValueError('APK signing certificate does not match the configured release certificate')
    command([build_tools / 'zipalign', '-c', '-P', '16', '4', path])
    output = command([build_tools / 'aapt', 'dump', 'badging', path])
    package = re.search(r"^package: name='([^']+)' versionCode='(\d+)'", output, re.M)
    if package is None or package[1] != PACKAGE or not 0 < int(package[2]) <= 2_100_000_000:
        raise ValueError('APK has an unexpected package or invalid versionCode')
    return int(package[2])


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stage(apk, output, build_tools, certificate, version_code=None):
    certificate = certificate.lower()
    if not re.fullmatch('[a-f0-9]{64}', certificate):
        raise ValueError('certificate-sha256 must be 64 hexadecimal characters')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.release-', dir=output) as temporary:
        snapshot = Path(temporary) / 'candidate.apk'
        with apk.open('rb') as source, snapshot.open('xb') as target:
            size = 0
            while block := source.read(65536):
                size += len(block)
                if size > MAX_APK:
                    raise ValueError('APK exceeds the phone download limit')
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        actual_version = inspect_apk(snapshot, build_tools, certificate)
        if version_code is not None and version_code != actual_version:
            raise ValueError('requested versionCode differs from the APK manifest')
        checksum = digest(snapshot)
        name = f'helmet-{actual_version}-{checksum[:16]}.apk'
        manifest = {'versionCode': actual_version, 'file': name, 'sha256': checksum,
                    'packageName': PACKAGE, 'certificateSha256': certificate}
        # Serialize publishers. Existing artifacts are immutable; manifest is the only replaced file.
        with (output / '.publish.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            latest = output / 'manifest.json'
            if latest.exists():
                previous = json.loads(latest.read_text())
                if previous.get('certificateSha256', certificate) != certificate:
                    raise ValueError('configured certificate differs from the previous release')
                old_version = previous['versionCode']
                if type(old_version) is not int or old_version <= 0:
                    raise ValueError('invalid existing release manifest')
                old_name = previous['file']
                if not isinstance(old_name, str) or not re.fullmatch(r'[a-zA-Z0-9._-]+\.apk', old_name):
                    raise ValueError('invalid existing artifact name')
                old_artifact = (output / old_name).resolve()
                if old_artifact.parent != output.resolve() or digest(old_artifact) != previous['sha256']:
                    raise ValueError('existing release artifact is missing or corrupted')
                if actual_version < old_version:
                    raise ValueError('release downgrade rejected')
                if actual_version == old_version:
                    if checksum != previous['sha256']:
                        raise ValueError('a different APK already uses this versionCode')
                    return previous
            artifact = output / name
            snapshot.chmod(0o444)
            try:
                os.link(snapshot, artifact)
            except FileExistsError:
                if artifact.is_symlink() or digest(artifact) != checksum:
                    raise ValueError('immutable release artifact conflicts with the APK')
            sync_directory(output)
            next_manifest = Path(temporary) / 'manifest.json'
            with next_manifest.open('x') as stream:
                stream.write(json.dumps(manifest, indent=2) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(next_manifest, latest)
            sync_directory(output)
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('apk', type=Path)
    parser.add_argument('--version-code', type=int, help='optional assertion; version is read from the APK')
    parser.add_argument('--certificate-sha256', required=True, help='expected signing certificate, provisioned separately')
    parser.add_argument('--build-tools', type=Path, required=True, help='Android SDK build-tools directory')
    parser.add_argument('--output', type=Path, default=Path('releases'))
    args = parser.parse_args()
    try:
        manifest = stage(args.apk, args.output, args.build_tools, args.certificate_sha256, args.version_code)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Release rejected: {error}\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
