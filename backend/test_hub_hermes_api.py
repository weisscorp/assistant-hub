import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from hub_hermes_api import endpoint, turn


class HermesAPITests(unittest.TestCase):
    def test_network_boundary(self):
        for url in ['http://example.com/v1', 'http://8.8.8.8/v1', 'http://0.0.0.0/v1',
                    'https://name:secret@example.com/v1', 'https://example.com/v1?key=x']:
            with self.assertRaises(ValueError): endpoint(url)
        self.assertEqual(endpoint('http://100.106.52.98:30432/v1'), 'http://100.106.52.98:30432/v1/chat/completions')

    def test_authenticated_bounded_history_and_stable_request_id(self):
        seen = {}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                seen.update(auth=self.headers.get('Authorization'), id=self.headers.get('Idempotency-Key'),
                            body=json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                data = json.dumps({'choices': [{'message': {'content': 'Connected'}}]}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as root:
                key = Path(root)/'key'; key.write_text('synthetic-private-token'); key.chmod(0o600)
                task_id = str(uuid.uuid4())
                body = {'task': {'id': task_id, 'prompt': 'Question'}, 'instructions': 'System',
                        'context': {'entries': [{'id': task_id, 'role': 'user', 'body': 'Question'},
                                               {'id': 'tool', 'role': 'tool', 'body': 'internal'}]}}
                self.assertEqual(turn({'url': 'http://127.0.0.1:'+str(server.server_port)+'/v1', 'key_file': str(key)}, body), 'Connected')
                self.assertEqual(seen['auth'], 'Bearer synthetic-private-token'); self.assertEqual(seen['id'], task_id)
                self.assertEqual(sum(m['content']=='Question' for m in seen['body']['messages']), 1)
                self.assertNotIn('internal', json.dumps(seen['body']))
        finally: server.shutdown(); server.server_close()
