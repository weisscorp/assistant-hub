import os
import json
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch
from hermes_agent import converse, prepare
from run_hermes import launch


class HermesTest(unittest.TestCase):
    def test_result_flags_close_and_literal_request(self):
        class Agent:
            result = {'completed': True, 'final_response': 'Готово'}
            closed = False
            def __init__(self, **options):
                self.options = options
                Agent.last = self
            def run_conversation(self, **options):
                self.request = options
                return self.result
            def close(self): self.closed = True
        text = 'Проверь $(literal); `не команда shell`'
        def run(): return converse(Agent, Path('/tmp/work'), text, 'model', 'http://127.0.0.1/v1', 'key', 3, 30, 'id')
        self.assertEqual('Готово', run())
        self.assertTrue(Agent.last.closed)
        self.assertEqual(text, Agent.last.request['user_message'])
        self.assertEqual(['terminal', 'file'], Agent.last.options['enabled_toolsets'])
        self.assertTrue(Agent.last.options['skip_background_review'])
        for result in ({'completed': False, 'final_response': 'fake success'},
                       {'completed': True, 'failed': True, 'final_response': 'failure'},
                       {'completed': True, 'interrupted': True, 'final_response': 'interrupted'},
                       {'completed': True, 'partial': True, 'final_response': 'partial'},
                       {'completed': True, 'error': 'secret', 'final_response': 'error'},
                       {'completed': True, 'final_response': ''}):
            Agent.result = result
            with self.assertRaises(ValueError): run()
            self.assertTrue(Agent.last.closed)

    def test_pinned_checkout_private_home_endpoint(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ), patch('sys.path', []):
            root = Path(directory) / 'hermes'; root.mkdir()
            work = Path(directory) / 'work'; work.mkdir()
            home = Path(directory) / 'state'
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
            git('init', '-q'); git('config', 'user.email', 'test@example.invalid'); git('config', 'user.name', 'test')
            (root / 'run_agent.py').write_text('# fixture\n')
            git('add', '.'); git('commit', '-qm', 'fixture')
            revision = git('rev-parse', 'HEAD')
            self.assertEqual(work.resolve(), prepare(root, revision, work, home, 'http://127.0.0.1:9000/v1', 'model'))
            self.assertEqual(0o700, home.stat().st_mode & 0o777)
            for url in ('http://remote.example/v1', 'https://key:secret@example.org/v1', 'https://example.org/v1?key=secret'):
                with self.assertRaises(ValueError): prepare(root, revision, work, home, url, 'model')
            with self.assertRaises(ValueError): prepare(root, '0' * 40, work, home, 'https://example.org/v1', 'model')
            home.chmod(0o755)
            with self.assertRaises(ValueError): prepare(root, revision, work, home, 'https://example.org/v1', 'model')
            home.chmod(0o700)
            (root / 'run_agent.py').write_text('# changed\n')
            with self.assertRaises(ValueError): prepare(root, revision, work, home, 'https://example.org/v1', 'model')

    def test_launcher_uses_fixed_arguments_and_private_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            root = Path(directory).resolve()
            config = root / 'agent.json'
            config.write_text(json.dumps({'hermes_root': str(root), 'workspace': str(root), 'home': str(root),
                                         'runtime_dir': str(root), 'bootstrap_python': sys.executable,
                                         'base_url': 'https://example.org/v1', 'model': 'model'}))
            config.chmod(0o600)
            os.environ['HELMET_MODEL_API_KEY'] = 'fixture-secret'
            os.environ['HELMET_JOB_ID'] = 'fixture-id'
            with patch('run_hermes.prepare', return_value=root), patch('run_hermes.subprocess.check_output',
                      side_effect=[json.dumps({'PATH': '/fixed/path'}), sys.executable]), patch('run_hermes.os.execve') as execute:
                launch(config)
                executable, argv, environment = execute.call_args.args
                self.assertEqual(sys.executable, executable)
                self.assertIn('--workspace', argv)
                self.assertNotIn('fixture-secret', argv)
                self.assertEqual('fixture-secret', environment['HELMET_MODEL_API_KEY'])
                self.assertEqual('fixture-id', environment['HELMET_JOB_ID'])
            config.chmod(0o644)
            with patch('run_hermes.prepare') as prepare_mock:
                with self.assertRaises(ValueError): launch(config)
                prepare_mock.assert_not_called()


if __name__ == '__main__': unittest.main()
