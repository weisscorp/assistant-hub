"""Opt-in development OTA: authenticated HTTPS artifacts and commit-based build publication.

Trusted owner commits only. The build worker is not a sandbox for hostile source code.
No ADB, phone control, audio or Google services are involved.
"""
import argparse
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import time

from build_release import build_release
from make_release import MAX_APK, digest, stage, sync_directory

ARTIFACT = re.compile(r'helmet-([1-9][0-9]*)-([a-f0-9]{16})\.apk')


def private_file(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 65536:
        raise ValueError('Expected an owned mode-0600 private file')
    return path


def load(path):
    return json.loads(private_file(path).read_text())


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path); sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


class Artifacts:
    """Hash each immutable release once, rather than reading hundreds of MB per poll."""
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        import threading
        self.lock = threading.Lock()
        self.verified = {}

    def open(self, name, expected=None):
        if not ARTIFACT.fullmatch(name): raise FileNotFoundError('Unknown artifact')
        descriptor = os.open(self.root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        stream = os.fdopen(descriptor, 'rb')
        try:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_APK or info.st_mode & 0o222:
                raise ValueError('Artifact must be an immutable bounded regular file')
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            with self.lock:
                checksum = self.verified.get(name)
                if checksum is None or checksum[0] != identity:
                    value = hashlib.sha256()
                    while block := stream.read(1024 * 1024): value.update(block)
                    checksum = (identity, value.hexdigest())
                    self.verified[name] = checksum
            if checksum[1][:16] != ARTIFACT.fullmatch(name)[2] or (expected and checksum[1] != expected):
                raise ValueError('Artifact checksum differs')
            stream.seek(0)
            return stream, info.st_size
        except BaseException:
            stream.close(); raise

    def manifest(self):
        with (self.root / 'manifest.json').open('rb') as stream:
            data = stream.read(16385)
        if len(data) > 16384: raise ValueError('Oversized manifest')
        value = json.loads(data)
        if (type(value['versionCode']) is not int or not 0 < value['versionCode'] <= 2_100_000_000 or
                value['packageName'] != 'local.voice' or not re.fullmatch('[a-f0-9]{64}', value['sha256']) or
                not re.fullmatch('[a-f0-9]{64}', value['certificateSha256'])):
            raise ValueError('Invalid release manifest')
        match = ARTIFACT.fullmatch(value['file'])
        if not match or int(match[1]) != value['versionCode']: raise ValueError('Artifact version differs')
        stream, size = self.open(value['file'], value['sha256']); stream.close()
        return {**value, 'sizeBytes': size, 'channel': 'dev'}


def bearer(token):
    if not isinstance(token, str) or not 32 <= len(token) <= 512 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError('Expected a random single-line OTA key')
    return ('Bearer ' + token).encode('ascii')


class Publisher:
    """Independent upload credential; release verification never trusts request metadata."""
    def __init__(self, token, build_tools, certificate):
        self.credential = bearer(token)
        self.build_tools = Path(build_tools)
        if not re.fullmatch('[a-f0-9]{64}', certificate):
            raise ValueError('Expected the provisioned signing certificate SHA-256')
        self.certificate = certificate
        self.lock = threading.Lock()


def handler(artifacts, token, publisher=None):
    credential = bearer(token)
    if publisher and hmac.compare_digest(credential, publisher.credential):
        raise ValueError('Download and publication keys must be different')
    class Handler(BaseHTTPRequestHandler):
        server_version = 'AssistantDevOTA/1'
        def setup(self):
            super().setup()
            self.connection.settimeout(30)
        def log_message(self, *_): pass
        def reply(self, code, body):
            data = json.dumps(body).encode()
            self.close_connection = True
            self.send_response(code); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data))); self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.send_header('X-Content-Type-Options', 'nosniff'); self.end_headers(); self.wfile.write(data)
        def framing(self):
            # Reject ambiguity before reading any body (including an empty TE header).
            names = [name.lower() for name in self.headers.keys()]
            return len(names) == len(set(names)) and 'Transfer-Encoding' not in self.headers
        def authorized(self, expected):
            headers = self.headers.get_all('Authorization', [])
            return len(headers) == 1 and hmac.compare_digest(headers[0].encode(), expected)
        def do_GET(self):
            if not self.framing() or self.headers.get('Content-Length') not in (None, '0'):
                self.reply(400, {'error': 'body_not_allowed'}); return
            if self.path == '/healthz': self.reply(200, {'status': 'ok'}); return
            download = self.authorized(credential)
            publishing = publisher and self.authorized(publisher.credential)
            if not download and not publishing:
                self.reply(401, {'error': 'unauthorized'}); return
            try:
                if self.path == '/updates/manifest': self.reply(200, artifacts.manifest()); return
                if not download: self.reply(403, {'error': 'download_key_required'}); return
                name = self.path.removeprefix('/updates/')
                if not self.path.startswith('/updates/') or not ARTIFACT.fullmatch(name):
                    self.reply(404, {'error': 'not_found'}); return
                # Older immutable artifacts stay available across concurrent publication.
                with artifacts.open(name)[0] as stream:
                    self.close_connection = True
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/vnd.android.package-archive')
                    self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
                    self.send_header('Cache-Control', 'private, max-age=31536000, immutable')
                    self.send_header('Connection', 'close')
                    self.send_header('X-Content-Type-Options', 'nosniff'); self.end_headers()
                    shutil.copyfileobj(stream, self.wfile, 65536)
            except FileNotFoundError: self.reply(404, {'error': 'not_found'})
            except (ValueError, KeyError, TypeError, OSError): self.reply(503, {'error': 'release_unavailable'})
        def do_POST(self):
            if not self.framing(): self.reply(400, {'error': 'ambiguous_headers'}); return
            if self.path != '/updates/publish' or publisher is None:
                self.reply(405, {'error': 'method_not_allowed'}); return
            if not self.authorized(publisher.credential):
                self.reply(401, {'error': 'publisher_key_required'}); return
            length = self.headers.get('Content-Length', '')
            version = self.headers.get('X-OTA-Version-Code', '')
            checksum = self.headers.get('X-OTA-SHA256', '')
            if (not re.fullmatch('[1-9][0-9]{0,9}', length) or not 0 < int(length) <= MAX_APK or
                    not re.fullmatch('[1-9][0-9]{0,9}', version) or not 0 < int(version) <= 2_100_000_000 or
                    not re.fullmatch('[a-f0-9]{64}', checksum) or 'Expect' in self.headers):
                self.reply(400, {'error': 'invalid_upload_metadata'}); return
            if not publisher.lock.acquire(blocking=False):
                self.reply(409, {'error': 'publication_busy'}); return
            try:
                code, body = self.receive_upload(int(length), int(version), checksum)
            except (TimeoutError, ConnectionError):
                code, body = 408, {'error': 'upload_timeout'}
            except (ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                conflict = str(error) in ('release downgrade rejected', 'a different APK already uses this versionCode')
                code, body = (409 if conflict else 422), {'error': 'release_conflict' if conflict else 'release_rejected'}
            except OSError:
                code, body = 503, {'error': 'publication_unavailable'}
            finally: publisher.lock.release()
            # Cleanup and release serialization before the receipt reaches a retrying client.
            try: self.reply(code, body)
            except OSError: pass
        def receive_upload(self, length, version, checksum):
            with tempfile.TemporaryDirectory(prefix='.upload-', dir=artifacts.root) as temporary:
                candidate = Path(temporary) / 'candidate.apk'
                actual = hashlib.sha256(); remaining = length; deadline = time.monotonic() + 900
                with candidate.open('xb') as target:
                    while remaining:
                        block = self.rfile.read(min(65536, remaining))
                        if not block or time.monotonic() > deadline:
                            return 400, {'error': 'incomplete_upload'}
                        target.write(block); actual.update(block); remaining -= len(block)
                    target.flush(); os.fsync(target.fileno())
                if not hmac.compare_digest(actual.hexdigest(), checksum):
                    return 422, {'error': 'checksum_mismatch'}
                manifest = stage(candidate, artifacts.root, publisher.build_tools, publisher.certificate, version)
                return 200, {**manifest, 'sizeBytes': length, 'channel': 'dev'}
    return Handler


def serve(config):
    host = str(ipaddress.ip_address(config['host']))
    address = ipaddress.ip_address(host)
    if not (address.is_loopback or address.is_private) or address.is_unspecified:
        raise ValueError('Dev OTA must bind an explicit private/VPN address')
    if address.version != 4: raise ValueError('This development listener currently supports IPv4')
    root = Path(config['output']); root.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = private_file(config['token_file']).read_text().strip()
    publisher = None
    if config.get('publisher_token_file'):
        publisher = Publisher(private_file(config['publisher_token_file']).read_text().strip(),
                              config['build_tools'], config['certificate_sha256'])
    server = ThreadingHTTPServer((host, int(config.get('port', 8779))), handler(Artifacts(root), token, publisher))
    server.daemon_threads = True
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(config['tls_cert'], private_file(config['tls_key']))
    # Defer each handshake to the request thread, whose socket timeout is already
    # set by setup(); one incomplete TLS client must not block the accept loop.
    server.socket = tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    try: server.serve_forever()
    finally: server.server_close()


def git(checkout, *args):
    return subprocess.check_output(['git', '-C', str(checkout), *args], text=True, timeout=120).strip()


def approved_commit(config):
    checkout = Path(config['checkout']).resolve(strict=True)
    branch = config.get('branch', 'main')
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9/._-]*', branch) or '..' in branch:
        raise ValueError('Invalid approved branch')
    revision = git(checkout, 'rev-parse', '--verify', 'refs/heads/' + branch)
    if git(checkout, 'rev-parse', 'HEAD') != revision or git(checkout, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('Publish only clean owner commits from the selected local branch')
    return checkout, revision


def forward_publication(config, state, state_path, published):
    remote = config.get('remote_publication')
    if not remote or not published: return
    if (state.get('remote_published_sha256') == published['sha256'] and
            state.get('remote_endpoint') == remote['endpoint']): return
    if time.time() < state.get('remote_retry_after', 0): return
    # Persist backoff before I/O. A lost response or worker restart is retried without
    # rebuilding an already signed commit; the server's exact retry is idempotent.
    state['remote_retry_after'] = time.time() + 60
    atomic_json(state_path, state)
    try:
        from dev_ota_publish import publish
        receipt = publish(config['output'], remote, config['certificate_sha256'])
        state.update(remote_published_sha256=receipt['sha256'], remote_version=receipt['versionCode'],
                     remote_endpoint=remote['endpoint'], remote_retry_after=0)
        state.pop('remote_error', None)
    except Exception as error:
        state['remote_error'] = type(error).__name__
        raise
    finally: atomic_json(state_path, state)


def build_once(config, builder=build_release):
    workspace = Path(config['workspace']).resolve(); workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (workspace / '.watch.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = workspace / 'build-state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        output = Path(config['output'])
        published = Artifacts(output).manifest() if (output / 'manifest.json').exists() else None
        if published and state.get('attempt_version') == published['versionCode'] and state.get('attempt_commit'):
            # Recover a crash between atomic publication and recording its receipt.
            state.update(published_commit=state['attempt_commit'], published_version=published['versionCode'], retry_after=0)
            atomic_json(state_path, state)
        forward_publication(config, state, state_path, published)
        checkout, revision = approved_commit(config)
        # Failures retry the same assigned version, with durable backoff. Never rebuild a published commit.
        if state.get('published_commit') == revision: return None
        if state.get('attempt_commit') == revision and time.time() < state.get('retry_after', 0): return None
        floor = max(int(config['minimum_version']), published['versionCode'] if published else 0)
        version = state.get('attempt_version') if state.get('attempt_commit') == revision else max(floor, state.get('attempt_version', 0)) + 1
        if type(version) is not int or not floor < version <= 2_100_000_000: raise ValueError('Invalid next version')
        state.update(attempt_commit=revision, attempt_version=version, started_at=time.time(), retry_after=time.time() + 300)
        atomic_json(state_path, state)
        try:
            source = workspace / 'source'
            if source == checkout or checkout in source.parents or source in checkout.parents:
                raise ValueError('Build workspace must be separate from the editable checkout')
            if not source.exists():
                subprocess.run(['git', 'clone', '--no-hardlinks', '--no-checkout', str(checkout), str(source)], check=True, timeout=120,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['git', '-C', str(source), 'fetch', '--quiet', str(checkout), revision], check=True, timeout=120)
            subprocess.run(['git', '-C', str(source), 'checkout', '--quiet', '--detach', '--force', revision], check=True, timeout=120)
            # Provisioned offline build inputs are outside Git and never uploaded by this worker.
            inputs = Path(config['prepared_inputs']).resolve(strict=True)
            if (inputs / 'dependencies.sha256').read_text().strip() != digest(source / 'dependencies.json'):
                raise ValueError('Prepared inputs do not match this commit dependency lock; re-provision inputs')
            for directory in ('vendor', 'app/libs'):
                incoming = inputs / directory
                if incoming.exists(): shutil.copytree(incoming, source / directory, dirs_exist_ok=True)
            assets = inputs / 'app/src/main/assets'
            tracked = set(git(source, 'ls-files').splitlines())
            for incoming in assets.rglob('*') if assets.exists() else []:
                relative = 'app/src/main/assets/' + incoming.relative_to(assets).as_posix()
                if incoming.is_symlink() or relative in tracked:
                    raise ValueError('Prepared assets may not replace tracked code/data')
                if incoming.is_file():
                    target = source / relative; target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(incoming, target)
            previous = dict(os.environ)
            credentials = load(config['credentials_file'])
            try:
                os.environ.clear()
                os.environ.update(PATH='/usr/bin:/bin', HOME=str(Path.home()), LANG='en_US.UTF-8',
                                  JAVA_HOME=config['java_home'], ANDROID_HOME=config['android_home'],
                                  KEYSTORE_PASSWORD=credentials['KEYSTORE_PASSWORD'], KEY_PASSWORD=credentials['KEY_PASSWORD'])
                manifest = builder(source, Path(config['build_tools']), Path(config['keystore']), config['alias'],
                                   config['certificate_sha256'], output, workspace / 'build.log', version)
            finally:
                os.environ.clear(); os.environ.update(previous)
            state.update(published_commit=revision, published_version=manifest['versionCode'], completed_at=time.time(), retry_after=0)
            atomic_json(state_path, state)
            forward_publication(config, state, state_path, manifest)
            return manifest
        except Exception as error:
            state.update(error=type(error).__name__); atomic_json(state_path, state)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve', 'build', 'watch', 'publish'])
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args(); config = load(args.config)
    if args.command == 'serve': serve(config); return
    if args.command == 'publish':
        from dev_ota_publish import publish
        publish(config['output'], config['remote_publication'], config['certificate_sha256'])
        print('Checked development release published.', flush=True); return
    if args.command == 'build': build_once(config); return
    while True:
        try: build_once(config)
        except Exception as error: print('Dev build deferred/failed: ' + type(error).__name__, flush=True)
        time.sleep(max(15, int(config.get('poll_seconds', 30))))


if __name__ == '__main__': main()
