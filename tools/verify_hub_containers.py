#!/usr/bin/env python3
"""Isolated Docker smoke test. No real model, user state or device is contacted."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('Docker command failed: ' + ' '.join(args[:3]) + '\n' + result.stderr[-3000:])
    return result.stdout.strip()


FIXTURE = '''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        value={'data': {'status': 'ready'}} if self.path=='/source' else {'models':[]}
        self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length',0)))
        report={'summary':'Synthetic event checked','severity':'info','confidence':'high','suggestions':[]}
        self.send_response(200);self.end_headers()
        self.wfile.write(json.dumps({'done':True,'message':{'content':json.dumps(report)}}).encode())
ThreadingHTTPServer(('0.0.0.0',11434),Handler).serve_forever()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hub-image', default='assistant-hub:dev-20261021.2')
    parser.add_argument('--collector-image', default='assistant-hub-collectors:dev-20261021')
    args = parser.parse_args()
    tag = 'hub-smoke-' + uuid.uuid4().hex[:10]
    model, hub = tag + '-model', tag + '-hub'
    containers, volumes = [], []
    owner = secrets.token_urlsafe(40)
    with tempfile.TemporaryDirectory(prefix=tag) as temporary:
        root = Path(temporary)
        # Synthetic, disposable fixture files only; accessible to container UID.
        root.chmod(0o755)
        (root / 'fixture.py').write_text(FIXTURE)
        (root / 'owner-token').write_text(owner)
        (root / 'hub.json').write_text(json.dumps({'providers': {'fixture': {'type': 'ollama',
            'url': 'http://model:11434', 'model': 'synthetic'}}, 'incidents': {'provider': 'fixture'}}))
        try:
            docker('network', 'create', tag)
            for volume in (tag + '-state', tag + '-outbox'):
                docker('volume', 'create', volume)
                volumes.append(volume)
            containers.append(model)
            docker('run', '-d', '--name', model, '--network', tag, '--network-alias', 'model',
                '--mount', 'type=bind,src=' + str(root) + ',dst=/fixtures,readonly',
                '--entrypoint', 'python3', args.hub_image, '/fixtures/fixture.py')
            containers.append(hub)
            docker('run', '-d', '--name', hub, '--network', tag, '--network-alias', 'hub',
                '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m,mode=1777', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '-p', '127.0.0.1::8778',
                '-e', 'HUB_OWNER_TOKEN_FILE=/fixtures/owner-token', '-e', 'HUB_CONFIG_FILE=/fixtures/hub.json',
                '-e', 'HUB_HTTP_ORIGINS=http://hub:8778', '-v', tag + '-state:/data',
                '--mount', 'type=bind,src=' + str(root) + ',dst=/fixtures,readonly', args.hub_image)
            published = docker('port', hub, '8778/tcp').splitlines()[0]
            url = 'http://' + published
            def request(path, token=owner, body=None):
                req = urllib.request.Request(url + path, data=body, headers={
                    'Host': 'hub:8778', 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as response:
                    raw = response.read()
                    return response.status, json.loads(raw) if raw else None
            def wait_for(predicate, timeout=25):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        value = predicate()
                        if value:
                            return value
                    except (OSError, urllib.error.URLError):
                        pass
                    time.sleep(.3)
                raise AssertionError('Container did not reach expected state')
            wait_for(lambda: request('/health')[1]['status'] == 'ok')
            docker('exec', hub, 'python3', 'hub_container.py', 'healthcheck')
            assert request('/api/incidents')[1] == []
            docker('exec', hub, 'python3', '../tools/hub_keys.py', '--config', '/tmp/assistant-hub/config.json',
                'create', 'synthetic-collector', '--scope', 'webhook', '--output', '/tmp/collector-token')
            # Some Docker backends cannot `docker cp` from a tmpfs mount.
            # Capture this synthetic secret directly into a private fixture file.
            (root / 'collector-token').write_text(docker('exec', hub, 'cat', '/tmp/collector-token'))
            (root / 'collector-token').chmod(0o644)
            producer = (root / 'collector-token').read_text().strip()
            (root / 'collectors.toml').write_text('''[hub]
url="http://hub:8778"
token_file="/fixtures/collector-token"
[runtime]
state="/data/collectors"
[[sources]]
id="synthetic-source"
plugin="hub_collector_plugins:HttpJson"
interval_seconds=1
timeout_seconds=5
[sources.options]
url="http://model:11434/source"
pointer="/data"
''')
            def collector(command='once'):
                return docker('run', '--rm', '--network', tag, '-v', tag + '-outbox:/data',
                    '--mount', 'type=bind,src=' + str(root) + ',dst=/fixtures,readonly',
                    args.collector_image, command, '--config', '/fixtures/collectors.toml')
            collector()
            cases = wait_for(lambda: request('/api/incidents')[1])
            case_id = cases[0]['id']
            case = wait_for(lambda: (lambda v: v if v['status'] == 'completed' else None)(request('/api/incidents/' + case_id)[1]))
            assert case['assessment']['summary'] == 'Synthetic event checked', case
            assert json.loads(case['payload'])['data'] == {'status': 'ready'}
            time.sleep(1.1)
            collector()
            assert len(request('/api/incidents')[1]) == 1, 'Unchanged source emitted another event'
            # Hub restart preserves events, key hashes and collector fingerprint/cursor state.
            docker('restart', '-t', '10', hub)
            url = 'http://' + docker('port', hub, '8778/tcp').splitlines()[0]
            wait_for(lambda: request('/health')[1]['status'] == 'ok')
            collector()
            assert len(request('/api/incidents')[1]) == 1
            try:
                request('/api/state', token=producer)
                raise AssertionError('Producer key could read owner state')
            except urllib.error.HTTPError as error:
                assert error.code == 403, error.code
            try:
                request('/webhooks/events', token=producer, body=b'{"data":NaN}')
                raise AssertionError('Invalid JSON was accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 400, error.code
            status = json.loads(collector('status'))
            assert status['outbox'] == {'delivered': 1}, status
            print(json.dumps({'passed': True, 'checks': ['non-root readonly container boot', 'authenticated healthcheck',
                'plugin subprocess -> durable outbox -> webhook -> synthetic model', 'unchanged data deduplication',
                'Hub and collector restart persistence', 'producer scope isolation', 'invalid JSON rejection'],
                'hub_image': args.hub_image, 'collector_image': args.collector_image}, ensure_ascii=False))
        except Exception:
            if hub in containers:
                logs = subprocess.run(['docker', 'logs', '--tail', '30', hub], capture_output=True, text=True)
                # Only synthetic fixture runtime is ever inspected by this script.
                print((logs.stdout + logs.stderr)[-3000:])
            raise
        finally:
            for name in reversed(containers):
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
            for name in volumes:
                subprocess.run(['docker', 'volume', 'rm', name], capture_output=True)
            subprocess.run(['docker', 'network', 'rm', tag], capture_output=True)


if __name__ == '__main__':
    main()
