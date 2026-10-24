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
    parser.add_argument('--hub-image', help='Use an already built Hub image (requires --collector-image too)')
    parser.add_argument('--collector-image', help='Use an already built collector image')
    parser.add_argument('--platform', default='linux/amd64')
    parser.add_argument('--unit-tests', action='store_true', help='Also run backend tests and benchmark inside disposable Compose service')
    args = parser.parse_args()
    if bool(args.hub_image) != bool(args.collector_image):
        parser.error('Supply both image options, or neither to build with Compose')
    tag = 'hub-smoke-' + uuid.uuid4().hex[:10]
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(HUB_TEST_HUB_IMAGE=args.hub_image or tag + '-hub',
        HUB_TEST_COLLECTOR_IMAGE=args.collector_image or tag + '-collectors',
        HUB_TEST_PLATFORM=args.platform)
    command = ['docker', 'compose', '--project-name', tag, '--file',
        str(repository / 'deploy/hub/compose.test.yaml')]
    def compose(*args, timeout=120):
        result = subprocess.run([*command, *args], env=environment, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError('Compose failed: ' + ' '.join(args[:3]) + '\n' + (result.stdout + result.stderr)[-5000:])
        return result.stdout.strip()
    owner = secrets.token_urlsafe(40)
    with tempfile.TemporaryDirectory(prefix=tag) as temporary:
        root = Path(temporary)
        # Synthetic, disposable fixture files only; accessible to container UID.
        root.chmod(0o755)
        environment['HUB_TEST_FIXTURES'] = str(root)
        (root / 'fixture.py').write_text(FIXTURE)
        (root / 'owner-token').write_text(owner)
        (root / 'hub.json').write_text(json.dumps({'providers': {'fixture': {'type': 'ollama',
            'url': 'http://model:11434', 'model': 'synthetic'}}, 'incidents': {'provider': 'fixture'}}))
        for fixture in root.iterdir():
            fixture.chmod(0o644)  # Disposable data must be readable by container UID 10001.
        try:
            if args.unit_tests:
                print(compose('run', '--rm', '--no-deps', 'unit', timeout=3600))
            if not args.hub_image:
                compose('build', 'hub', 'collectors', timeout=600)
            compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '90', 'model', 'hub')
            published = compose('port', 'hub', '8778').splitlines()[0]
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
            compose('exec', '-T', 'hub', 'python3', 'hub_container.py', 'healthcheck')
            assert request('/api/incidents')[1] == []
            compose('exec', '-T', 'hub', 'python3', '../tools/hub_keys.py', '--config', '/tmp/assistant-hub/config.json',
                'create', 'synthetic-collector', '--scope', 'webhook', '--output', '/tmp/collector-token')
            # Read the disposable key from its private container runtime directory.
            # Capture this synthetic secret directly into a private fixture file.
            (root / 'collector-token').write_text(compose('exec', '-T', 'hub', 'cat', '/tmp/collector-token'))
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
            (root / 'collectors.toml').chmod(0o644)
            def collector(command='once'):
                return compose('run', '--rm', '--no-deps', 'collectors', command, '--config', '/fixtures/collectors.toml')
            compose('up', '-d', '--no-build', 'collectors')
            cases = wait_for(lambda: request('/api/incidents')[1])
            case_id = cases[0]['id']
            case = wait_for(lambda: (lambda v: v if v['status'] == 'completed' else None)(request('/api/incidents/' + case_id)[1]))
            assert case['assessment']['summary'] == 'Synthetic event checked', case
            assert json.loads(case['payload'])['data'] == {'status': 'ready'}
            compose('stop', '-t', '10', 'collectors')
            time.sleep(1.1)
            collector()
            assert len(request('/api/incidents')[1]) == 1, 'Unchanged source emitted another event'
            # Hub restart preserves events, key hashes and collector fingerprint/cursor state.
            compose('restart', '-t', '10', 'hub')
            url = 'http://' + compose('port', 'hub', '8778').splitlines()[0]
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
                'project': tag, 'platform': args.platform,
                'hub_image': environment['HUB_TEST_HUB_IMAGE'], 'collector_image': environment['HUB_TEST_COLLECTOR_IMAGE']}, ensure_ascii=False))
        except Exception:
            # Logs contain synthetic fixtures only.
            print(compose('logs', '--tail', '30'))
            raise
        finally:
            # Scoped to a random project; never prunes or stops other host containers.
            if args.unit_tests:
                compose('--profile', 'unit', 'down', '-v', '--remove-orphans', timeout=120)
            else:
                compose('down', '-v', '--remove-orphans', timeout=120)


if __name__ == '__main__':
    main()
