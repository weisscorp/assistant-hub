"""Forward a locally checked, signed immutable OTA release over verified HTTPS."""
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import ssl
from urllib.parse import urlsplit

from dev_ota import Artifacts, bearer, private_file


class RemotePublishError(ValueError):
    """Deliberately carries only a non-secret diagnostic category."""


def publish(output, remote, certificate):
    manifest = Artifacts(output).manifest()
    if manifest['certificateSha256'] != certificate:
        raise RemotePublishError('Local release certificate differs from configured identity')
    endpoint = urlsplit(remote['endpoint'])
    if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or
            endpoint.query or endpoint.fragment or endpoint.path not in ('', '/')):
        raise RemotePublishError('Expected an HTTPS publication origin without credentials or path')
    key = bearer(private_file(remote['token_file']).read_text().strip()).decode('ascii')
    context = ssl.create_default_context(cafile=str(Path(remote['tls_cert']).resolve(strict=True)))
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    def connection():
        return http.client.HTTPSConnection(endpoint.hostname, endpoint.port or 443, context=context, timeout=90)

    def receipt(response):
        data = response.read(16385)
        if len(data) > 16384: raise RemotePublishError('Oversized publication receipt')
        if response.status != 200: raise RemotePublishError('Remote publication refused (HTTP ' + str(response.status) + ')')
        try: value = json.loads(data)
        except (ValueError, UnicodeError): raise RemotePublishError('Invalid publication receipt') from None
        required = ('versionCode', 'file', 'sha256', 'packageName', 'certificateSha256', 'sizeBytes', 'channel')
        if not isinstance(value, dict) or any(value.get(name) != manifest[name] for name in required):
            raise RemotePublishError('Publication receipt differs from checked local artifact')
        return value

    # Retry a lost receipt without sending hundreds of MB again. A different version
    # still goes through the server's full signature/package/alignment validation.
    conn = connection()
    try:
        conn.request('GET', '/updates/manifest', headers={'Authorization': key})
        response = conn.getresponse()
        if response.status == 200:
            data = response.read(16385)
            if len(data) > 16384: raise RemotePublishError('Oversized remote manifest')
            try: existing = json.loads(data)
            except (ValueError, UnicodeError): raise RemotePublishError('Invalid remote manifest') from None
            if not isinstance(existing, dict): raise RemotePublishError('Invalid remote manifest')
            if existing.get('certificateSha256') != certificate:
                raise RemotePublishError('Remote signing identity differs')
            version = existing.get('versionCode')
            if type(version) is not int: raise RemotePublishError('Invalid remote version')
            if version > manifest['versionCode']: raise RemotePublishError('Remote release is newer than local release')
            if version == manifest['versionCode']:
                required = ('versionCode', 'file', 'sha256', 'packageName', 'certificateSha256', 'sizeBytes', 'channel')
                if any(existing.get(name) != manifest[name] for name in required):
                    raise RemotePublishError('Remote version conflicts with local release')
                return existing
        elif response.status == 404:
            response.read(16385)  # Empty release store is expected on initial deployment.
        else:
            raise RemotePublishError('Remote manifest refused (HTTP ' + str(response.status) + ')')
    finally: conn.close()

    # Re-open the immutable file and recheck it while sending; never buffer an APK.
    conn = connection()
    store = Artifacts(output)
    try:
        with store.open(manifest['file'], manifest['sha256'])[0] as stream:
            conn.putrequest('POST', '/updates/publish')
            for name, value in {'Authorization': key, 'Content-Type': 'application/vnd.android.package-archive',
                                'Content-Length': str(manifest['sizeBytes']),
                                'X-OTA-Version-Code': str(manifest['versionCode']),
                                'X-OTA-SHA256': manifest['sha256']}.items():
                conn.putheader(name, value)
            conn.endheaders()
            actual = hashlib.sha256(); size = 0
            while block := stream.read(65536):
                conn.send(block); actual.update(block); size += len(block)
            if size != manifest['sizeBytes'] or not hmac.compare_digest(actual.hexdigest(), manifest['sha256']):
                raise RemotePublishError('Local immutable release changed during transmission')
            return receipt(conn.getresponse())
    finally: conn.close()
