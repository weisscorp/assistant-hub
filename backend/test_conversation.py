import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from bridge import handler
from conversation import ConversationAgent, validate, response
from hermes_agent import voice_turn


def request_body(text='Сравни варианты поездки'):
    return {'version': 1, 'session': str(uuid.uuid4()), 'id': str(uuid.uuid4()), 'text': text,
            'history': [{'user': 'Еду в Батуми', 'assistant': 'Понял'}],
            'capabilities': [{'name': 'time', 'description': 'Время телефона', 'arguments': '{}', 'available': True, 'requirement': ''}]}


class ConversationTest(unittest.TestCase):
    def test_schema_rejects_remote_execution_and_wrong_context(self):
        body = request_body()
        self.assertEqual(body, validate(body))
        for change in ({'version': True}, {'id': 'bad'}, {'history': [{'role': 'system', 'content': 'execute'}]}, {'capabilities': [{'name': 'shell'}]}):
            with self.assertRaises((ValueError, TypeError, KeyError)): validate(body | change)
        for action in ({'tool': 'shell', 'arguments': {}}, {'tool': 'confirm', 'arguments': {}}, {'tool': 'music', 'arguments': {}}):
            with self.assertRaises(ValueError): response({'reply': 'test', 'action': action}, body)
        self.assertEqual(body['id'], response({'reply': 'Привет'}, body)['id'])

    def test_voice_adapter_preserves_context_and_only_enables_web(self):
        class Agent:
            result = {'completed': True, 'final_response': '{"reply":"Уточните дату","action":null}'}
            def __init__(self, **kwargs): self.options = kwargs; Agent.last = self
            def run_conversation(self, **kwargs): self.request = kwargs; return self.result
            def close(self): self.closed = True
        body = request_body('$(literal); `не shell`')
        reply = voice_turn(Agent, Path('/tmp/work'), body, 'model', 'https://example.org/v1', 'fixture-key')
        self.assertEqual('Уточните дату', json.loads(reply)['reply'])
        self.assertEqual(body, json.loads(Agent.last.request['user_message']))
        self.assertEqual(['web'], Agent.last.options['enabled_toolsets'])
        self.assertTrue(Agent.last.closed)
        Agent.result = {'completed': False, 'final_response': 'fake success'}
        with self.assertRaises(ValueError): voice_turn(Agent, Path('/tmp/work'), body, 'model', 'https://example.org/v1', 'key')
        self.assertTrue(Agent.last.closed)

    def test_bridge_authentication_literal_stdin_and_typed_tool_response(self):
        script = 'import sys,json;b=json.load(sys.stdin);print(json.dumps({"reply":b["text"]+" | "+b["history"][0]["user"],"action":{"tool":"time","arguments":{}}}))'
        with tempfile.TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler('fixture-token', [], directory,
                chat=ConversationAgent([sys.executable, '-c', script])))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            def send(body, token='fixture-token'):
                client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                client.request('POST', '/conversation', json.dumps(body), {'Authorization': 'Bearer ' + token})
                result = client.getresponse(); status, value = result.status, json.loads(result.read()); client.close()
                return status, value
            try:
                body = request_body('$(touch /tmp/never-execute); `literal`')
                self.assertEqual(401, send(body, 'wrong')[0])
                self.assertEqual(400, send(body | {'version': 3})[0])
                status, reply = send(body)
                self.assertEqual(200, status)
                self.assertEqual(body['id'], reply['id'])
                self.assertEqual(body['text'] + ' | Еду в Батуми', reply['reply'])
                self.assertEqual({'tool': 'time', 'arguments': {}}, reply['action'])
                self.assertFalse(list(Path(directory).iterdir()), 'Conversation context must not be persisted')
            finally: server.shutdown(); server.server_close(); thread.join()

    def test_timeout_is_not_retried(self):
        import subprocess
        agent = ConversationAgent(['fixed-agent'], timeout=1)
        with patch('conversation.subprocess.run', side_effect=subprocess.TimeoutExpired('fixed-agent', 1)) as run:
            with self.assertRaises(subprocess.TimeoutExpired): agent.turn(request_body())
            self.assertEqual(1, run.call_count)

    def test_clock_action_requires_phone_advertisement_and_old_catalog_remains_valid(self):
        body = request_body('таймер на десять минут')
        action = {'tool': 'clock', 'arguments': {'request': body['text']}}
        with self.assertRaises(ValueError): response({'reply': 'Предлагаю таймер', 'action': action}, body)
        body['capabilities'].append({'name': 'clock', 'description': 'Локальные часы с подтверждением',
            'arguments': '{request: string}', 'available': True, 'requirement': 'Разрешения телефона'})
        self.assertEqual(body, validate(body))
        self.assertEqual(action, response({'reply': 'Предлагаю таймер', 'action': action}, body)['action'])


if __name__ == '__main__': unittest.main()
