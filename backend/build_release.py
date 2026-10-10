"""Trusted build/sign/stage entry point. Keystore and passwords are external configuration.
Run separately from the coding agent; this is not a sandbox for untrusted source code.
"""
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile
from make_release import stage

BUILD_TASKS = [':app:assembleRelease', ':app:testReleaseUnitTest', ':app:lintRelease']
SECRET_NAMES = ('ASSISTANT_TOKEN', 'TLS_KEY', 'TLS_CERT', 'KEYSTORE_PASSWORD', 'KEY_PASSWORD', 'HELMET_MODEL_API_KEY')


class BuildChecksFailed(subprocess.CalledProcessError):
    def __init__(self, error, log):
        super().__init__(error.returncode, error.cmd)
        with log.open('rb') as stream:
            stream.seek(max(0, log.stat().st_size - 16000))
            text = stream.read(16000).decode('utf-8', errors='replace')
        self.diagnostics = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)[-4000:]


def build_failure_report(error, version):
    if not isinstance(error, BuildChecksFailed): return None
    return {'kind': 'build_checks_failed', 'versionCode': version, 'diagnostics': error.diagnostics}


@contextmanager
def checkout_lock(path, identity=None):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        owners = {os.geteuid()} | ({identity.pw_uid} if identity else set())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid not in owners:
            raise ValueError('unsafe checkout lock')
        if identity and info.st_uid != identity.pw_uid:
            os.fchown(descriptor, identity.pw_uid, identity.pw_gid)
            os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally: os.close(descriptor)


def build_release(checkout, build_tools, keystore, alias, certificate, output, log, version_code=None, build_user=None):
    checkout = checkout.resolve()
    keystore = keystore.resolve(strict=True)
    if not alias.strip():
        raise ValueError('keystore alias is required')
    if version_code is not None and (type(version_code) is not int or not 0 < version_code <= 2_100_000_000):
        raise ValueError('invalid requested versionCode')
    version_arguments = [] if version_code is None else [f'-PhelmetVersionCode={version_code}', f'-PhelmetVersionName=0.{version_code}.0']
    identity = pwd.getpwnam(build_user) if build_user else None
    if identity and (identity.pw_uid == 0 or identity.pw_uid == os.geteuid() or os.geteuid() != 0):
        raise ValueError('separate build identity requires a root launcher and a non-root build user')
    if not os.environ.get('KEYSTORE_PASSWORD') or not os.environ.get('KEY_PASSWORD'):
        raise ValueError('set KEYSTORE_PASSWORD and KEY_PASSWORD in the release process environment')
    unsigned = checkout / 'app/build/outputs/apk/release/app-release-unsigned.apk'
    # Fixed tasks/paths; the agent request never becomes a command, Gradle argument or output path.
    with checkout_lock(checkout / '.release-build.lock', identity):
        unsigned.unlink(missing_ok=True)
        build_environment = {name: value for name, value in os.environ.items() if name not in SECRET_NAMES}
        build_identity = {}
        if identity:
            build_environment.update(HOME=identity.pw_dir, USER=identity.pw_name, LOGNAME=identity.pw_name)
            build_identity = dict(user=identity.pw_uid, group=identity.pw_gid, extra_groups=[])
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log.open('w') as stream:
                subprocess.run([str(checkout / 'gradlew'), *BUILD_TASKS, *version_arguments, '--no-daemon'], cwd=checkout,
                               env=build_environment, stdout=stream, stderr=subprocess.STDOUT,
                               timeout=3600, check=True, shell=False, **build_identity)
        except subprocess.CalledProcessError as error:
            raise BuildChecksFailed(error, log) from None
        if not unsigned.is_file():
            raise ValueError('build did not produce the expected unsigned release APK')
        with tempfile.TemporaryDirectory(prefix='helmet-sign-') as temporary:
            aligned = Path(temporary) / 'aligned.apk'
            signed = Path(temporary) / 'signed.apk'
            subprocess.run([str(build_tools / 'zipalign'), '-P', '16', '-f', '4', str(unsigned), str(aligned)],
                           check=True, timeout=120, shell=False, stdout=subprocess.DEVNULL)
            sign_environment = dict(build_environment)
            sign_environment['KEYSTORE_PASSWORD'] = os.environ['KEYSTORE_PASSWORD']
            sign_environment['KEY_PASSWORD'] = os.environ['KEY_PASSWORD']
            subprocess.run([str(build_tools / 'apksigner'), 'sign', '--ks', str(keystore), '--ks-key-alias', alias,
                            '--ks-pass', 'env:KEYSTORE_PASSWORD', '--key-pass', 'env:KEY_PASSWORD',
                            '--out', str(signed), str(aligned)], env=sign_environment,
                           check=True, timeout=120, shell=False, stdout=subprocess.DEVNULL)
            return stage(signed, output, build_tools, certificate, version_code=version_code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--build-tools', type=Path, required=True)
    parser.add_argument('--keystore', type=Path, required=True)
    parser.add_argument('--alias', required=True)
    parser.add_argument('--certificate-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--version-code', type=int, help='trusted server-assigned build version; checked against the signed APK')
    parser.add_argument('--build-user', help='run Gradle as a separate non-root user; launcher must be root')
    args = parser.parse_args()
    try:
        manifest = build_release(args.checkout, args.build_tools.resolve(), args.keystore, args.alias,
                                 args.certificate_sha256, args.output, args.log, args.version_code, args.build_user)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        from json import dumps
        report = build_failure_report(error, args.version_code)
        if report: print(dumps(report, ensure_ascii=False))
        parser.exit(1, f'Build/release failed: {error}\n')
    import json
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__': main()
