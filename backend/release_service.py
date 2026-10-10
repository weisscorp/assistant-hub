"""Provisioned privileged release entry point: fixed private config, build UID separation.
Only versionCode is caller-controlled. No request text, path, command or secret in argv.
Install this service code outside the agent's writable checkout, owned by root.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import sys


def trusted_path(path):
    path = Path(path).resolve(strict=True)
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('release code/tools must be root-owned and not writable by other users')
    return path


# The deployment command uses Python -I. Add only the provisioned service directory.
if __name__ == '__main__':
    trusted_path(Path(__file__).parent)
    for module in Path(__file__).parent.glob('*.py'): trusted_path(module)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_release import build_release, build_failure_report
from development_agent import verify_publication


def private_file(path):
    path = Path(path).absolute()
    if path.is_symlink(): raise ValueError('private configuration cannot be a symlink')
    info = path.stat()
    directory = path.parent.stat()
    if not path.is_file() or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 16384:
        raise ValueError('private file must be small, owned by the release identity, with mode 0600')
    if directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
        raise ValueError('private file directory must be owned by release identity with mode 0700')
    return path.resolve()


def private_keystore(path):
    path = Path(path).absolute()
    if path.is_symlink(): raise ValueError('keystore cannot be a symlink')
    info = path.stat(); directory = path.parent.stat()
    if not path.is_file() or info.st_uid != 0 or info.st_mode & 0o077 or directory.st_uid != 0 or directory.st_mode & 0o077:
        raise ValueError('keystore and its directory must be private and root-owned')
    return path.resolve()


def release(config_path, version):
    if os.geteuid() != 0: raise ValueError('release launcher must run as root to drop build privileges')
    if type(version) is not int or not 0 < version <= 2_100_000_000: raise ValueError('invalid requested version')
    config = json.loads(private_file(config_path).read_text())
    build = pwd.getpwnam(config['build_user'])
    if build.pw_uid == 0: raise ValueError('build user must be unprivileged')
    checkout = Path(config['checkout']).resolve(strict=True)
    if checkout.stat().st_uid != build.pw_uid: raise ValueError('checkout must belong to the configured build user')
    credentials_path = private_file(config['credentials_file'])
    keystore = private_keystore(config['keystore'])
    for path in (credentials_path, keystore.resolve(), Path(config_path).resolve()):
        if path == checkout or checkout in path.parents: raise ValueError('release secrets/config must be outside the editable checkout')
    credentials = json.loads(credentials_path.read_text())
    for name in ('KEYSTORE_PASSWORD', 'KEY_PASSWORD'):
        if not isinstance(credentials.get(name), str) or not credentials[name]: raise ValueError('missing signing credential')
    java = trusted_path(config['java_home'])
    sdk = trusted_path(config['android_home'])
    tools = trusted_path(config['build_tools'])
    for executable in ('apksigner', 'zipalign', 'aapt'): trusted_path(tools / executable)
    trusted_path(tools / 'lib/apksigner.jar')
    trusted_path(java / 'bin/java')
    output = Path(config['output']).absolute()
    logs = Path(config['log_dir']).absolute()
    trusted_path(output.parent); trusted_path(logs.parent)
    output.mkdir(mode=0o755, exist_ok=True); logs.mkdir(mode=0o700, exist_ok=True)
    trusted_path(output); trusted_path(logs)
    floor = config['minimum_version']
    if type(floor) is not int or not 0 < floor < 2_100_000_000: raise ValueError('invalid version floor')
    with (output / '.release-request.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = verify_publication(output, tools, config['certificate_sha256']) if (output / 'manifest.json').exists() else None
        if current and version == current['versionCode']: return current  # Receipt retry, never rebuild a used version.
        if version != max(floor, current['versionCode'] if current else 0) + 1:
            raise ValueError('only the next server-assigned version may be built')
        previous = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(PATH='/usr/bin:/bin', JAVA_HOME=str(java), ANDROID_HOME=str(sdk), LANG='C.UTF-8',
                              KEYSTORE_PASSWORD=credentials['KEYSTORE_PASSWORD'], KEY_PASSWORD=credentials['KEY_PASSWORD'])
            return build_release(checkout, tools, keystore, config['alias'], config['certificate_sha256'],
                                 output.resolve(), logs.resolve() / f'release-{version}.log', version, config['build_user'])
        finally:
            os.environ.clear(); os.environ.update(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--version-code', type=int, required=True)
    args = parser.parse_args()
    try: manifest = release(args.config, args.version_code)
    except Exception as error:
        report = build_failure_report(error, args.version_code)
        if report: print(json.dumps(report, ensure_ascii=False))
        print(f'Release service refused ({type(error).__name__}); inspect private service configuration/build log.', file=sys.stderr)
        return 1
    print(json.dumps(manifest))
    return 0


if __name__ == '__main__': sys.exit(main())
