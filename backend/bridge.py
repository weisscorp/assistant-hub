"""Private bridge to a user-configured server agent. No agent is installed by this script.
Serve behind HTTPS, or set TLS_CERT/TLS_KEY. Keep signing/build jobs outside this process.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import ssl
import signal
import time
import uuid
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit, parse_qs
from jobs import JobStore, Conflict, QueueFull, job_id
from conversation import ConversationAgent, Busy, validate as validate_conversation
from home_link import HomeLinkStore, RevisionConflict, MAX_IMAGE, MAX_SNAPSHOT, identifier


class BridgeServer(ThreadingHTTPServer):
    def handle_error(self, request, address):
        pass  # Invalid TLS/closed sockets must not print request material to stderr.


def handler(token, agent_argv, release_dir, jobs=None, chat=None, link=None):
    root = Path(release_dir).resolve()
    release_lock = threading.Lock()
    release_cache = {}
    def notify_release(device, installed):
        if link is None or installed <= 0: return
        try:
            with release_lock:
                manifest_file = root / 'manifest.json'
                manifest = json.loads(manifest_file.read_bytes())
                name = manifest['file']; version = manifest['versionCode']
                if type(version) is not int or version <= installed: return
                if Path(name).name != name or not name.endswith('.apk'): return
                artifact = (root / name).resolve()
                if artifact.parent != root: return
                info = artifact.stat()
                signature = (name, info.st_size, info.st_mtime_ns, manifest['sha256'])
                if release_cache.get('signature') != signature:
                    digest = hashlib.sha256()
                    with artifact.open('rb') as stream:
                        for data in iter(lambda: stream.read(65536), b''): digest.update(data)
                    if digest.hexdigest() != manifest['sha256']: return
                    release_cache['signature'] = signature
                identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"phone-release:{version}:{manifest['sha256']}"))
                label = str(manifest.get('versionName', version))[:80]
            link.publish(device, dict(id=identifier, kind='update', source='Обновления Hermes', title='Доступна новая версия',
                body=f'На сервере доступна версия {label}. Чтобы проверить её, скажите: проверь обновления. Установка требует вашего запроса.',
                priority='normal', createdAt=int(time.time()*1000)))
        except (OSError, ValueError, KeyError, TypeError): pass # Bad release must never block the message inbox.


    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            pass  # Never persist commands, transcripts, or credentials.

        def send_json(self, status, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError): pass

        def authorized(self):
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode('utf-8'), ('Bearer ' + token).encode('utf-8')):
                self.send_json(401, {'error': 'unauthorized'})
                return False
            return True

        def home_link(self, method):
            if not self.path.startswith('/link/'): return False
            if link is None:
                self.send_json(503, {'error': 'link_not_configured'}); return True
            try:
                url = urlsplit(self.path)
                parts = url.path.split('/')
                device = identifier(parts[2]); route = parts[3:]
                query = parse_qs(url.query, strict_parsing=True)
                if route == ['events'] and method == 'GET':
                    if set(query) - {'after', 'wait'} or any(len(v) != 1 for v in query.values()): raise ValueError()
                    notify_release(device, int(self.headers.get('X-Assistant-Version-Code', '0')))
                    result = link.events(device, int(query.get('after', ['0'])[0]), int(query.get('wait', ['0'])[0]))
                    self.send_json(200, result); return True
                if query: raise ValueError()
                if method == 'GET' and route == ['knowledge']:
                    self.send_json(200, link.knowledge(device)); return True
                if method == 'GET' and len(route) == 2 and route[0] == 'image':
                    data = link.image(device, route[1])
                    self.send_response(200); self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data); return True
                if method != 'POST':
                    self.send_json(404, {'error': 'not_found'}); return True
                image = len(route) == 2 and route[0] == 'image'
                if not image and route not in [['knowledge'], ['events'], ['ack']]:
                    self.send_json(404, {'error': 'not_found'}); return True
                size = int(self.headers.get('Content-Length', '0'))
                limit = MAX_IMAGE if image else MAX_SNAPSHOT if route == ['knowledge'] else 16384
                if not 0 < size <= limit:
                    self.send_json(413, {'error': 'body_too_large'}); return True
                raw = self.rfile.read(size)
                if len(raw) != size: raise ValueError()
                if image: result = link.image(device, route[1], raw)
                else:
                    body = json.loads(raw)
                    if route == ['knowledge']: result = link.sync(device, body)
                    elif route == ['events']: result = link.publish(device, body)
                    else:
                        if not isinstance(body, dict) or set(body) != {'through'}: raise ValueError()
                        result = link.ack(device, body['through'])
                self.send_json(200, result)
            except RevisionConflict as error: self.send_json(409, {'error': 'revision_conflict', 'revision': error.revision})
            except (KeyError, StopIteration): self.send_json(404, {'error': 'not_found'})
            except (ValueError, TypeError, IndexError, OSError): self.send_json(400, {'error': 'invalid_link_request'})
            return True

        def do_POST(self):
            if not self.authorized():
                return
            if self.home_link('POST'): return
            if self.path not in ('/assistant', '/jobs', '/conversation'):
                self.send_json(404, {'error': 'not_found'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= (65536 if self.path == '/conversation' else 16384):
                    self.send_json(413, {'error': 'body_too_large'})
                    return
                body = json.loads(self.rfile.read(size))
                if self.path == '/conversation':
                    validate_conversation(body)
                    device = self.headers.get('X-Assistant-Device')
                    if device is not None and link is not None:
                        body = dict(body, phoneDevice=identifier(device))
                    if chat is None:
                        self.send_json(503, {'error': 'conversation_not_configured'})
                        return
                    try: self.send_json(200, chat.turn(body))
                    except Busy: self.send_json(429, {'error': 'conversation_busy'})
                    except (subprocess.SubprocessError, OSError, ValueError, TypeError, KeyError):
                        self.send_json(502, {'error': 'conversation_failed'})
                    return
                text = body['text']
                if not isinstance(text, str) or not 0 < len(text.strip()) <= 2000:
                    raise ValueError('invalid text')
            except (ValueError, KeyError, TypeError, OSError):
                self.send_json(400, {'error': 'invalid_request'})
                return
            if not agent_argv:
                self.send_json(503, {'error': 'agent_not_configured'})
                return
            if self.path == '/jobs':
                if jobs is None:
                    self.send_json(503, {'error': 'queue_not_configured'})
                    return
                try:
                    result = jobs.submit(job_id(body['id']), text)
                    self.send_json(202, result)
                except Conflict:
                    self.send_json(409, {'error': 'id_conflict'})
                except QueueFull:
                    self.send_json(429, {'error': 'queue_full'})
                except (ValueError, KeyError, TypeError):
                    self.send_json(400, {'error': 'invalid_request'})
                return
            # The argv is trusted configuration. User text is stdin, never shell code/argv.
            try:
                result = subprocess.run(agent_argv, input=text, text=True, capture_output=True,
                                        timeout=25, check=True, shell=False)
                reply = result.stdout.strip()[:1500]
                if not reply:
                    raise ValueError('empty agent reply')
                self.send_json(200, {'reply': reply})
            except (subprocess.SubprocessError, OSError, ValueError):
                self.send_json(502, {'error': 'agent_failed'})

        def job(self, cancel=False):
            if jobs is None:
                self.send_json(503, {'error': 'queue_not_configured'})
                return
            try:
                identifier = job_id(self.path[len('/jobs/'):])
                result = jobs.cancel(identifier) if cancel else jobs.get(identifier)
                self.send_json(200 if result else 404, result or {'error': 'not_found'})
            except (ValueError, TypeError):
                self.send_json(400, {'error': 'invalid_job_id'})
            except QueueFull:
                self.send_json(429, {'error': 'queue_full'})

        def do_DELETE(self):
            if not self.authorized():
                return
            if self.path.startswith('/jobs/'):
                self.job(cancel=True)
            else:
                self.send_json(404, {'error': 'not_found'})

        def do_GET(self):
            if not self.authorized():
                return
            if self.home_link('GET'): return
            if self.path.startswith('/jobs/'):
                self.job()
                return
            try:
                if self.path == '/updates/manifest':
                    manifest = json.loads((root / 'manifest.json').read_text())
                    name = manifest['file']
                    if Path(name).name != name or not name.endswith('.apk'):
                        raise ValueError('invalid artifact name')
                    artifact = (root / name).resolve()
                    if artifact.parent != root:
                        raise ValueError('artifact escapes root')
                    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                    if digest != manifest['sha256']:
                        raise ValueError('artifact checksum mismatch')
                    self.send_json(200, manifest)
                    return
                if self.path.startswith('/updates/'):
                    name = unquote(self.path[len('/updates/'):])
                    manifest = json.loads((root / 'manifest.json').read_text())
                    artifact = (root / name).resolve()
                    if name != manifest['file'] or artifact.parent != root or not name.endswith('.apk'):
                        self.send_json(404, {'error': 'not_found'})
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/vnd.android.package-archive')
                    self.send_header('Content-Length', str(artifact.stat().st_size))
                    self.end_headers()
                    with artifact.open('rb') as stream:
                        while data := stream.read(65536):
                            self.wfile.write(data)
                    return
                self.send_json(404, {'error': 'not_found'})
            except (OSError, ValueError, KeyError, TypeError):
                self.send_json(503, {'error': 'release_unavailable'})
    return Handler


def main():
    token = os.environ['ASSISTANT_TOKEN']
    if len(token) < 32:
        raise ValueError('Use a random token of at least 32 characters')
    argv = json.loads(os.environ.get('AGENT_COMMAND_JSON', '[]'))
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise ValueError('AGENT_COMMAND_JSON must be a JSON array of strings')
    jobs = JobStore(argv, os.environ.get('JOB_STATE_DIR', './job-state'), timeout=float(os.environ.get('JOB_TIMEOUT', '1800')))
    chat_argv = json.loads(os.environ.get('CHAT_COMMAND_JSON', '[]'))
    if not isinstance(chat_argv, list) or any(not isinstance(item, str) for item in chat_argv):
        raise ValueError('CHAT_COMMAND_JSON must be an argv array')
    chat = ConversationAgent(chat_argv) if chat_argv else None
    link_path = os.environ.get('HOME_LINK_STATE_DIR', './home-link-state')
    os.environ['HOME_LINK_STATE_DIR'] = str(Path(link_path).resolve())
    link = HomeLinkStore(link_path)
    server = None
    def stopped(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stopped)
    try:
        server = BridgeServer((os.environ.get('BIND', '127.0.0.1'), int(os.environ.get('PORT', '8765'))),
                              handler(token, argv, os.environ.get('RELEASE_DIR', './releases'), jobs, chat, link))
        if os.environ.get('TLS_CERT'):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(os.environ['TLS_CERT'], os.environ['TLS_KEY'])
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None: server.server_close()
        jobs.close()


if __name__ == '__main__':
    main()
