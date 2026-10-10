import hashlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import time
import uuid
from http.server import ThreadingHTTPServer
from bridge import handler
from jobs import JobStore


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.artifact = b'test artifact, not an APK'
        (self.root / 'next.apk').write_bytes(self.artifact)
        (self.root / 'manifest.json').write_text(json.dumps({'file': 'next.apk', 'versionCode': 3, 'sha256': hashlib.sha256(self.artifact).hexdigest()}))
        self.jobs = JobStore([sys.executable, '-c', 'import sys;print(sys.stdin.read())'], self.root / 'jobs')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler('test-token', [sys.executable, '-c', 'import sys;print(sys.stdin.read())'], self.root, self.jobs))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.jobs.close(); self.directory.cleanup()

    def request(self, method, path, body=None, token='test-token'):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={'Authorization': 'Bearer ' + token})
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_authorization_applies_to_agent_and_downloads(self):
        for method, path in [('POST', '/assistant'), ('GET', '/updates/manifest'), ('GET', '/updates/next.apk')]:
            self.assertEqual(401, self.request(method, path, {'text': 'hello'}, token='wrong')[0])

    def test_text_is_literal_stdin(self):
        text = '$(touch /tmp/should-not-exist); `echo test`'
        status, data = self.request('POST', '/assistant', {'text': text})
        self.assertEqual(200, status)
        self.assertEqual(text, json.loads(data)['reply'])

    def test_invalid_and_oversized_requests(self):
        for body in [{'text': []}, {'text': ''}, {'text': 'x' * 2001}]:
            self.assertEqual(400, self.request('POST', '/assistant', body)[0])
        self.assertEqual(413, self.request('POST', '/assistant', {'text': 'x' * 17000})[0])

    def test_update_download_is_confined_and_manifest_verifies_bytes(self):
        self.assertEqual(200, self.request('GET', '/updates/manifest')[0])
        self.assertEqual((200, self.artifact), self.request('GET', '/updates/next.apk'))
        self.assertEqual(404, self.request('GET', '/updates/%2e%2e/secret.apk')[0])
        (self.root / 'next.apk').write_bytes(b'changed')
        self.assertEqual(503, self.request('GET', '/updates/manifest')[0])

    def test_job_rest_authorization_validation_and_unknown_cancellation(self):
        key = str(uuid.uuid4())
        for method, path in [('POST', '/jobs'), ('GET', '/jobs/' + key), ('DELETE', '/jobs/' + key)]:
            self.assertEqual(401, self.request(method, path, {'id': key, 'text': 'test'}, token='wrong')[0])
        self.assertEqual(400, self.request('POST', '/jobs', {'id': '../../escape', 'text': 'test'})[0])
        self.assertEqual(400, self.request('GET', '/jobs/not-a-uuid')[0])
        self.assertEqual(404, self.request('GET', '/jobs/' + key)[0])
        status, data = self.request('DELETE', '/jobs/' + key)
        self.assertEqual(200, status)
        self.assertEqual('cancelled', json.loads(data)['state'])
        self.assertEqual('cancelled', json.loads(self.request('POST', '/jobs', {'id': key, 'text': 'delayed'})[1])['state'])

    def test_job_rest_literal_result_conflict_and_idempotent_retry(self):
        key = str(uuid.uuid4()); text = 'Русский запрос $(literal)'
        status, data = self.request('POST', '/jobs', {'id': key, 'text': text})
        self.assertEqual(202, status)
        self.assertEqual(key, json.loads(data)['id'])
        self.assertEqual(202, self.request('POST', '/jobs', {'id': key, 'text': text})[0])
        self.assertEqual(409, self.request('POST', '/jobs', {'id': key, 'text': 'different'})[0])
        until = time.monotonic() + 3
        while time.monotonic() < until:
            status, data = self.request('GET', '/jobs/' + key)
            value = json.loads(data)
            if value['state'] == 'succeeded': break
            time.sleep(.025)
        self.assertEqual(200, status)
        self.assertEqual('succeeded', value['state'])
        self.assertEqual(text, value['reply'])
        self.assertNotIn('text', value)
        self.assertEqual('succeeded', json.loads(self.request('DELETE', '/jobs/' + key)[1])['state'])


if __name__ == '__main__': unittest.main()
