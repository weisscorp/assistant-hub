"""Verification-command orchestration tests: subprocesses and AO are mocked."""
import importlib.util
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'verify_ao_overview', Path(__file__).resolve().parents[1] / 'tools/verify_ao_overview.py')
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


class VerificationTests(unittest.TestCase):
    def run_command(self, network=False, full_suite=False, tests_rc=0, snapshot=None):
        with patch.object(verification.subprocess, 'run', return_value=types.SimpleNamespace(returncode=tests_rc)) as run, \
                patch.object(verification, 'AOOverview') as source, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            source.return_value.snapshot.return_value = snapshot
            result = verification.verify(network, full_suite)
            return result, json.loads(output.getvalue()), run.call_args, source.call_count

    def test_default_never_contacts_live_source_or_runs_socket_tests(self):
        result, summary, call, reads = self.run_command()
        self.assertEqual(result, 0)
        self.assertEqual(reads, 0)
        self.assertNotIn('backend.test_hub_hermes_api', call.args[0])
        self.assertEqual(summary['live_ao']['result'], 'not_run')

    def test_network_runs_loopback_and_only_prints_live_summary(self):
        snapshot = {'source_status': 'available', 'checked_at': 100, 'last_success_at': 100,
                    'tasks': [{'title': 'PRIVATE TASK', 'task_direction': 'PRIVATE PROMPT'}]}
        result, summary, call, reads = self.run_command(network=True, snapshot=snapshot)
        self.assertEqual(result, 0)
        self.assertEqual(reads, 1)
        self.assertIn('backend.test_hub_hermes_api', call.args[0])
        self.assertEqual(summary['live_ao']['session_count'], 1)
        self.assertNotIn('PRIVATE', json.dumps(summary))

    def test_test_failure_does_not_skip_live_check_and_fails_command(self):
        snapshot = {'source_status': 'available', 'checked_at': 100, 'last_success_at': 100, 'tasks': []}
        result, summary, call, reads = self.run_command(True, True, 1, snapshot)
        self.assertEqual(result, 1)
        self.assertEqual(reads, 1)
        self.assertIn('discover', call.args[0])
        self.assertEqual(summary['tests_exit_code'], 1)

    def test_unavailable_live_source_is_a_failed_check(self):
        snapshot = {'source_status': 'unavailable', 'checked_at': 100, 'last_success_at': None,
                    'tasks': [], 'error': 'URLError'}
        result, summary, _, _ = self.run_command(network=True, snapshot=snapshot)
        self.assertEqual(result, 1)
        self.assertIsNone(summary['live_ao']['last_success_at'])
        self.assertEqual(summary['live_ao']['error'], 'URLError')
