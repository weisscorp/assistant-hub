"""Non-root Kubernetes entry point: projected credentials become owned private files."""
import os
from pathlib import Path
import stat

from dev_ota import private_file, serve


def configuration(environment=os.environ):
    secret = Path(environment.get('OTA_SECRET_DIR', '/var/run/ota-secrets'))
    private = Path(environment.get('OTA_PRIVATE_DIR', '/run/ota/identity'))
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = private.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('OTA private directory must belong to service UID')
    private.chmod(0o700)
    # Kubernetes Secret paths are symlinks owned by root and readable through fsGroup.
    # Open only this fixed whitelist, then copy into the service-owned emptyDir.
    for name in ('ota.key', 'publisher.key', 'tls.pem', 'tls.key', 'certificate.sha256'):
        with (secret / name).open('rb') as stream:
            value = stream.read(65537)
        if not value or len(value) > 65536: raise ValueError('Invalid projected OTA credential')
        descriptor = os.open(private / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
        private_file(private / name)
    return {'host': environment['POD_IP'], 'port': int(environment.get('OTA_PORT', '8443')),
            'output': environment.get('OTA_OUTPUT', '/data/releases'),
            'token_file': str(private / 'ota.key'), 'publisher_token_file': str(private / 'publisher.key'),
            'tls_cert': str(private / 'tls.pem'), 'tls_key': str(private / 'tls.key'),
            'certificate_sha256': (private / 'certificate.sha256').read_text().strip(),
            'build_tools': environment.get('OTA_BUILD_TOOLS', '/opt/android-build-tools')}


if __name__ == '__main__': serve(configuration())
