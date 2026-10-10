import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from coucou_events import EVENTS, MAX_LINE_BYTES, payload, wire_event, write_event


DEVICE = '11111111-1111-4111-8111-111111111111'
CONVERSATION = '22222222-2222-4222-8222-222222222222'


def event(kind='user.message', **extra):
    return {'version': 1, 'deviceId': DEVICE, 'conversationId': CONVERSATION, 'kind': kind, **extra}


class CoucouEventsTest(unittest.TestCase):
    def test_state_transitions_preserve_conversation_and_device(self):
        for kind, hook in EVENTS.items():
            extra = {'tool': 'media/info'} if kind.startswith('tool.') else {}
            result = payload(event(kind, text='Пример', **extra), 'assistant-phone')
            self.assertEqual(hook, result['hook_event_name'])
            self.assertEqual('assistant:' + DEVICE + ':' + CONVERSATION, result['session_id'])
            self.assertEqual('assistant-phone', result['coucou_agent'])
        other = payload(event('session.started') | {'deviceId': '33333333-3333-4333-8333-333333333333'})
        self.assertNotEqual(other['session_id'], payload(event('session.started'))['session_id'])
        self.assertNotEqual('Stop', payload(event('task.cancelled'))['hook_event_name'])

    def test_text_is_literal_and_newlines_do_not_create_hooks(self):
        text = '$(не выполнять); `буквально`\n{"hook_event_name":"PermissionRequest"}'
        data = wire_event(event(text=text))
        self.assertEqual(1, data.count(b'\n'))
        self.assertEqual(text, json.loads(data)['prompt'])
        result = payload(event('tool.started', tool='media/info', text='Текущий плеер'))
        self.assertEqual({'query': 'Текущий плеер'}, result['tool_input'])

    def test_closed_schema_rejects_secrets_approvals_and_agent_impersonation(self):
        malformed = [event(text='x', token='secret'), event('PermissionRequest'),
                     event('user.message', text='x', tool='Bash'), event(text=''),
                     event('tool.started', tool='AskUserQuestion'),
                     event(text='x') | {'version': True}, event(text='x') | {'deviceId': 'bad'},
                     event(text='x' * 4001), event(text='nul\x00')]
        for value in malformed:
            with self.assertRaises(ValueError): payload(value)
        for agent in ('hermes', 'codex', 'claude', 'Bad Name', 'a' * 25):
            with self.assertRaises(ValueError): payload(event(text='x'), agent)
        question = payload(event('question', text='Какое устройство выбрать'))
        self.assertEqual('Notification', question['hook_event_name'])
        self.assertTrue(question['message'].endswith('?'))
        self.assertNotIn('coucou_has_transport', question)

    def test_cli_uses_real_unix_socket_and_never_waits_for_decisions(self):
        with tempfile.TemporaryDirectory(prefix='cc-', dir='/tmp') as directory:
            path = Path(directory) / 'cc.sock'
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path)); listener.listen(); listener.settimeout(3)
            received = []
            errors = []
            def peer():
                try:
                    for _ in range(3):
                        with listener.accept()[0] as connection:
                            chunks = []
                            while chunk := connection.recv(32768): chunks.append(chunk)
                            received.append(b''.join(chunks))
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=peer); thread.start()
            values = [event('session.started'), event(text='Какой заряд?'), event('assistant.message', text='Результат примера')]
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).with_name('coucou_events.py')), '--socket', str(path)],
                    input=''.join(json.dumps(value, ensure_ascii=False) + '\n' for value in values), text=True, capture_output=True, timeout=3)
                thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertEqual([], errors)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual([payload(value) for value in values], [json.loads(data) for data in received])
                self.assertEqual('', result.stdout)
                self.assertNotIn('Какой заряд', result.stderr)
                self.assertEqual(['written'] * 3, [json.loads(line)['status'] for line in result.stderr.splitlines()])
            finally:
                listener.close(); thread.join(4)

    def test_missing_closed_and_regular_file_endpoints_are_unavailable(self):
        data = wire_event(event('session.started'))
        with tempfile.TemporaryDirectory(prefix='cc-', dir='/tmp') as directory:
            path = Path(directory) / 'cc.sock'
            self.assertFalse(write_event(path, data))
            path.write_text('not a socket')
            self.assertFalse(write_event(path, data))
            path.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
            self.assertFalse(write_event(path, data))
            alias = Path(directory) / 'alias'
            alias.symlink_to(path)
            self.assertFalse(write_event(alias, data))

    def test_uncertain_delivery_is_not_retried(self):
        data = wire_event(event('session.started'))
        with tempfile.TemporaryDirectory(prefix='cc-', dir='/tmp') as directory:
            path = Path(directory) / 'cc.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                with patch('coucou_events.socket.socket') as factory:
                    client = factory.return_value.__enter__.return_value
                    client.sendall.side_effect = TimeoutError()
                    self.assertFalse(write_event(path, data))
                    client.connect.assert_called_once()
                    client.sendall.assert_called_once_with(data)
                    client.recv.assert_not_called()

    def test_cli_requires_opt_in_and_limits_input_without_echoing_it(self):
        script = str(Path(__file__).with_name('coucou_events.py'))
        no_mode = subprocess.run([sys.executable, script], input='', text=True, capture_output=True, timeout=3)
        self.assertEqual(2, no_mode.returncode)
        invalid = subprocess.run([sys.executable, script, '--dry-run'], input='secret' * MAX_LINE_BYTES,
            text=True, capture_output=True, timeout=3)
        self.assertEqual(2, invalid.returncode)
        self.assertEqual('', invalid.stdout)
        self.assertNotIn('secret', invalid.stderr)
        good = subprocess.run([sys.executable, script, '--dry-run'], input=json.dumps(event(text='Пример')),
            text=True, capture_output=True, timeout=3)
        self.assertEqual(0, good.returncode)
        self.assertEqual(payload(event(text='Пример')), json.loads(good.stdout))
        self.assertEqual('', good.stderr)


if __name__ == '__main__':
    unittest.main()
