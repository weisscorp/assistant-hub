"""Disposable loopback server; run in the isolated Compose test container."""
from http.server import ThreadingHTTPServer
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
import uuid

from mini_tasks import Gateway, make_handler
from test_task_review import FixtureTasks


class ReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = FixtureTasks()
        self.gateway = Gateway(self.temp.name, self.native)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(
            self.gateway, 'synthetic-owner', 'synthetic-mcp', 'synthetic-hermes'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def post(self, body, path='/v1/reviews', token='synthetic-owner', headers=None):
        request = urllib.request.Request(self.url + path, json.dumps(body).encode(),
                  {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token, **(headers or {})})
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_owner_api_auth_revisions_restart_and_no_model_write(self):
        start = {'topic': 'synthetic-topic', 'action': 'start', 'operation': str(uuid.uuid4())}
        self.assertEqual(self.post(start, token='synthetic-mcp')[0], 401)
        self.assertEqual(self.post(start, token='wrong')[0], 401)
        status, review = self.post(start)
        self.assertEqual(status, 200)
        self.assertEqual(self.post(start)[1]['id'], review['id'])
        change = {'topic': 'synthetic-topic', 'session': review['id'], 'action': 'next',
                  'revision': review['revision'], 'operation': str(uuid.uuid4())}
        self.assertEqual(self.post(change)[0], 200)
        self.assertEqual(self.post(dict(change, operation=str(uuid.uuid4())))[0], 409)
        self.assertEqual(self.post({'topic': 'synthetic-topic', 'action': 'read'})[1]['cursor'], 1)
        # The read-only MCP key cannot invoke the owner mutation endpoint.
        self.assertEqual(self.post(change, token='synthetic-mcp')[0], 401)
        self.assertEqual(self.post({'topic': 'other-topic', 'action': 'read', 'session': review['id']})[0], 400)
        self.assertFalse(self.native.calls)

    def test_chat_route_handles_explicit_review_without_hermes(self):
        def chat(prompt):
            return {'messages': [{'role': 'user', 'content': 'Hub context (data):\n{"context":{"id":"synthetic-topic"}}'},
                                 {'role': 'user', 'content': prompt}]}
        rid = str(uuid.uuid4())
        # A direct owner command must not call the live Hermes URL.
        with patch('mini_tasks.urllib.request.urlopen', side_effect=AssertionError('No Hermes')):
            # Use an independent opener for the synthetic client so the patch only
            # covers the gateway's fallback HTTP call.
            request = urllib.request.Request(self.url + '/v1/chat/completions', json.dumps(chat('Давай по задачам пройдёмся')).encode(),
                {'Authorization': 'Bearer synthetic-owner', 'Idempotency-Key': rid, 'Content-Type': 'application/json'})
            with urllib.request.build_opener().open(request, timeout=5) as response:
                result = json.load(response)
        self.assertEqual(result['review']['cursor'], 0)
        # Completed request replay returns the saved response and never another session.
        self.assertEqual(self.post(chat('Давай по задачам пройдёмся'), '/v1/chat/completions',
                         headers={'Idempotency-Key': rid})[1]['review']['id'], result['review']['id'])

    def test_start_outage_is_explicit_with_last_success(self):
        self.native.unavailable = True
        status, body = self.post({'topic': 'synthetic-topic', 'action': 'start', 'operation': str(uuid.uuid4())})
        self.assertEqual(status, 503)
        self.assertEqual(body['error'], 'источник недоступен')
        self.assertEqual(body['last_success_at'], 90)


if __name__ == '__main__':
    unittest.main()
