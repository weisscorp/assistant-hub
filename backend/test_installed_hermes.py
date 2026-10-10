import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from installed_hermes import source_fingerprint, model_endpoint, read_config, check_local_model, prepare, run


class InstalledHermesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.library = self.root / 'vendor'; self.library.mkdir()
        (self.library / 'run_agent.py').write_text('# installed fixture\n')
        self.workspace = self.root / 'work'; self.workspace.mkdir()
        count, digest = source_fingerprint(self.library)
        self.data = dict(hermes_root=str(self.library), source_files=count, source_sha256=digest,
                         workspace=str(self.workspace), home=str(self.root / 'home'), model='generic-model',
                         base_url='http://127.0.0.1:9000/v1')
        self.file = self.root / 'runtime.json'

    def tearDown(self): self.temp.cleanup()

    def write_config(self, data=None):
        self.file.write_text(json.dumps(self.data if data is None else data))
        self.file.chmod(0o600)

    def test_endpoint_requires_explicit_private_lan_ip_opt_in(self):
        for url in ('https://provider.example/v1', 'http://localhost:30068/v1', 'http://[::1]:30068/v1'):
            self.assertEqual(url, model_endpoint(url))
        for url in ('http://192.168.50.20:30068/v1', 'http://10.2.1.3/v1', 'http://[fd00::2]/v1'):
            with self.assertRaises(ValueError): model_endpoint(url)
            self.assertEqual(url, model_endpoint(url, True))
        for url in ('http://public.example/v1', 'http://8.8.8.8/v1', 'http://192.0.2.1/v1',
                    'http://169.254.1.1/v1', 'http://0.0.0.0/v1', 'http://224.0.0.1/v1',
                    'https://key:secret@provider.example/v1', 'https://provider.example/v1?token=secret'):
            with self.assertRaises(ValueError): model_endpoint(url, True)

    def test_source_pin_detects_changes_additions_and_symlinks(self):
        expected = source_fingerprint(self.library)
        (self.library / '__pycache__').mkdir()
        (self.library / '__pycache__/cache.py').write_text('ignored')
        (self.library / '.venv').mkdir()
        (self.library / '.venv/dependency.py').write_text('ignored')
        self.assertEqual(expected, source_fingerprint(self.library))
        (self.library / 'extra.py').write_text('# different code')
        self.assertNotEqual(expected, source_fingerprint(self.library))
        (self.library / 'extra.py').unlink()
        (self.library / 'link.py').symlink_to(self.library / 'run_agent.py')
        with self.assertRaises(ValueError): source_fingerprint(self.library)

    def test_private_config_rejects_arbitrary_commands_and_bad_budget(self):
        self.write_config()
        self.assertEqual(self.data, read_config(self.file))
        self.file.chmod(0o644)
        with self.assertRaises(ValueError): read_config(self.file)
        for change in ({'command': 'arbitrary'}, {'source_files': True}, {'source_sha256': 'bad'},
                       {'seconds': False}, {'max_iterations': 101}, {'device_file': '/private/phone.json'},
                       {'hermes_root': 'relative'}, {'api_key_env': '$(literal)'}):
            self.write_config(self.data | change)
            with self.assertRaises(ValueError): read_config(self.file)

    def test_changed_library_is_rejected_before_runtime_environment_changes(self):
        (self.library / 'run_agent.py').write_text('# changed')
        with patch.dict(os.environ), patch('sys.path', []):
            before = dict(os.environ)
            with self.assertRaises(ValueError): prepare(self.data)
            self.assertEqual(before, dict(os.environ))

    def test_missing_binding_never_inherits_or_guesses_a_device(self):
        data = self.data | {'link_state': str(self.root / 'state'), 'device_file': str(self.root / 'missing.json')}
        with patch.dict(os.environ, {'HOME_LINK_DEVICE': str(uuid.uuid4()), 'HOME_LINK_STATE_DIR': '/inherited'}), patch('sys.path', []):
            self.assertEqual(self.workspace, prepare(data, {'phoneDevice': str(uuid.uuid4())}))
            self.assertNotIn('HOME_LINK_STATE_DIR', os.environ)
            self.assertNotIn('HOME_LINK_DEVICE', os.environ)

    def test_owner_binding_rejects_cross_phone_requests(self):
        device = str(uuid.uuid4()); binding = self.root / 'phone.json'
        binding.write_text(json.dumps({'device': device}))
        data = self.data | {'link_state': str(self.root / 'state'), 'device_file': str(binding)}
        with patch.dict(os.environ), patch('sys.path', []):
            with self.assertRaises(ValueError): prepare(data, {'phoneDevice': str(uuid.uuid4())})
            self.assertEqual(self.workspace, prepare(data, {'phoneDevice': device}))
            self.assertEqual(device, os.environ['HOME_LINK_DEVICE'])

    def test_voice_preserves_literal_context_validates_completion_and_closes(self):
        class Agent:
            result = {'completed': True, 'final_response': '{"reply":"Уточните время","action":null}'}
            def __init__(self, **kwargs): self.options = kwargs; Agent.last = self
            def run_conversation(self, **kwargs): self.input = kwargs; return self.result
            def close(self): self.closed = True
        body = dict(version=1, session=str(uuid.uuid4()), id=str(uuid.uuid4()),
                    text='$(literal); `не shell`', history=[{'user': 'Краткая просьба', 'assistant': 'Понял'}], capabilities=[])
        with patch.dict(os.environ, clear=True):
            reply = run(Agent, self.workspace, self.data, json.dumps(body), conversation=True)
            self.assertEqual('Уточните время', json.loads(reply)['reply'])
            self.assertEqual(body, json.loads(Agent.last.input['user_message']))
            self.assertTrue(Agent.last.options['skip_context_files'])
            self.assertEqual(['web', 'no_mcp'], Agent.last.options['enabled_toolsets'])
            self.assertEqual(4, Agent.last.options['max_iterations'])
            self.assertTrue(Agent.last.closed)
            Agent.result = {'completed': True, 'partial': True, 'final_response': 'fake success'}
            with self.assertRaises(ValueError): run(Agent, self.workspace, self.data, json.dumps(body), conversation=True)
            self.assertTrue(Agent.last.closed)

    def test_remote_provider_cannot_inherit_a_local_dummy_credential(self):
        with patch.dict(os.environ, clear=True):
            with self.assertRaises(ValueError):
                run(lambda **kwargs: self.fail('Must reject before creating agent'), self.workspace,
                    self.data | {'base_url': 'https://provider.example/v1'}, 'request', conversation=True)

    def test_local_listener_probe_detects_refusal_without_sending_model_data(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            data = self.data | {'base_url': 'http://127.0.0.1:' + str(listener.getsockname()[1]) + '/v1'}
            with self.assertRaises(RuntimeError): check_local_model(data)
            listener.listen(1)
            check_local_model(data)
            client, _ = listener.accept()
            with client:
                client.settimeout(2)
                self.assertEqual(b'', client.recv(1), 'Probe must not send credentials or a request')
        with patch('installed_hermes.socket.create_connection') as connect:
            check_local_model(self.data | {'base_url': 'https://provider.example/v1'})
            connect.assert_not_called()

    def test_cli_refused_model_fails_before_import_and_sdk_retry(self):
        # A real closed listener, real CLI and valid pin/config. Importing the
        # vendor module would deliberately raise a different exception.
        (self.library / 'run_agent.py').write_text('raise AssertionError("must not import")\n')
        count, digest = source_fingerprint(self.library)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            data = self.data | {'source_files': count, 'source_sha256': digest,
                'base_url': 'http://127.0.0.1:' + str(listener.getsockname()[1]) + '/v1'}
            self.write_config(data)
            body = dict(version=1, session=str(uuid.uuid4()), id=str(uuid.uuid4()),
                text='Краткий вопрос', history=[], capabilities=[])
            started = time.monotonic()
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('installed_hermes.py')),
                '--config', str(self.file), '--conversation'], input=json.dumps(body), text=True,
                capture_output=True, timeout=5)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(1, result.returncode)
            self.assertEqual('', result.stdout)
            self.assertIn('(RuntimeError)', result.stderr)
            self.assertNotIn('AssertionError', result.stderr)
            self.assertFalse(Path(data['home']).exists())


if __name__ == '__main__': unittest.main()
