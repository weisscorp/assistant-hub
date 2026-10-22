"""Synthetic AO HTTP JSON fixtures; no daemon, device or service access."""
from copy import deepcopy
import io
from http.client import IncompleteRead
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import urllib.error

from ao_overview import AOOverview, instant, normalize, source_ref
from mini_tasks import Gateway, Tasks, TOOLS


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/ao_sessions.json').read_text())
NOW = instant('2026-10-22T10:05:00Z')


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.source = AOOverview('http://localhost:3001', clock=lambda: NOW)

    def response(self, payload=FIXTURE):
        return patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(payload).encode()))

    def test_read_only_all_sessions_and_shape(self):
        with self.response() as opened, patch.object(subprocess, 'run', side_effect=AssertionError('no CLI writes')):
            result = self.source.snapshot()
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertEqual(request.full_url, 'http://localhost:3001/api/v1/sessions')
        self.assertEqual(opened.call_args.kwargs['timeout'], 5)
        self.assertIsNone(request.data)
        self.assertEqual(result['source_status'], 'available')
        self.assertEqual(result['last_success_at'], NOW)
        self.assertEqual(len(result['tasks']), 6)
        self.assertEqual([r['status'] for r in result['tasks']],
                         ['working', 'waiting_user', 'blocked', 'completed', 'stale', 'unknown'])
        row = result['tasks'][0]
        self.assertEqual(row['source'], {'kind': 'ao', 'project_id': 'demo', 'session_id': 'demo-1', 'link': 'ao://sessions/demo/demo-1'})
        self.assertEqual(row['executor'], {'session_id': 'demo-1', 'harness': 'codex'})
        self.assertEqual(row['last_confirmed_action'], 'Fixture tests passed.')
        self.assertEqual(row['action_evidence'], 'assistant_report')
        self.assertEqual(row['task_direction'], 'Implement the example')
        self.assertIsNone(row['expected_result'])
        self.assertEqual(row['observed_at'], NOW)
        self.assertEqual(row['next_step_kind'], 'suggested')
        self.assertTrue(result['tasks'][2]['blocker'])
        self.assertTrue(result['tasks'][1]['expected_decision'])
        self.assertEqual(result['tasks'][4]['last_progress_at'], instant('2026-10-21T08:00:00Z'))

    def test_failure_and_recovery_never_present_cache_as_live(self):
        with self.response():
            self.source.snapshot()
        self.source.clock = lambda: NOW + 20
        for error in [TimeoutError(), urllib.error.URLError('private endpoint'),
                      IncompleteRead(b'private partial body')]:
            with patch.object(self.source, 'read_sessions', side_effect=error):
                result = self.source.snapshot()
            self.assertEqual(result['source_status'], 'unavailable')
            self.assertEqual(result['source_status_label'], 'источник недоступен')
            self.assertEqual(result['last_success_at'], NOW)
            self.assertEqual(result['checked_at'], NOW + 20)
            self.assertFalse(result['is_live'])
            self.assertNotIn('private', json.dumps(result))
            for row in result['tasks']:
                self.assertFalse(row['is_live'])
                self.assertEqual(row['status'], 'unknown')
                self.assertEqual(row['observed_at'], NOW)
            self.assertEqual(result['tasks'][0]['previous_status'], 'working')
        with self.response({'sessions': []}):
            result = self.source.snapshot()
        self.assertEqual(result['last_success_at'], NOW + 20)
        self.assertEqual(result['tasks'], [])
        self.assertTrue(result['is_live'])

    def test_projectless_session_does_not_break_other_projects(self):
        with self.response({'sessions': [*FIXTURE['sessions'], {'id': 'standalone'}]}):
            result = self.source.snapshot()
        self.assertEqual(result['source_status'], 'available')
        row = result['tasks'][-1]
        self.assertEqual(row['ref'], 'ao:/standalone')
        self.assertIsNone(row['source']['project_id'])
        self.assertIsNone(row['source']['link'])

    def test_first_failure_has_no_success_or_cache(self):
        with patch.object(self.source, 'read_sessions', side_effect=TimeoutError()):
            result = self.source.snapshot()
        self.assertIsNone(result['last_success_at'])
        self.assertEqual(result['tasks'], [])

    def test_malformed_or_oversized_responses_are_unavailable(self):
        for payload in [{}, [], {'sessions': 'bad'}, {'sessions': [None]},
                        {'sessions': [{'id': 'x', 'projectId': 123}]}, {'sessions': [dict(FIXTURE['sessions'][0], activity=[])]},
                        {'sessions': [FIXTURE['sessions'][0]] * 2}]:
            with self.subTest(payload=payload), self.response(payload):
                self.assertEqual(self.source.snapshot()['source_status'], 'unavailable')
        for data in [b'invalid json', b' ' * 4_000_001]:
            with patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(data)):
                self.assertEqual(self.source.snapshot()['source_status'], 'unavailable')

    def test_empty_null_slice_and_cache_cannot_be_modified_by_caller(self):
        with self.response({'sessions': None}):
            self.assertEqual(self.source.snapshot()['tasks'], [])
        with self.response():
            self.source.snapshot()['tasks'][0]['status'] = 'corrupted'
        with patch.object(self.source, 'read_sessions', side_effect=TimeoutError()):
            self.assertEqual(self.source.snapshot()['tasks'][0]['previous_status'], 'working')

    def test_private_config_and_validation(self):
        with patch.dict('os.environ', {'MINI_TASKS_AO_URL': 'https://ao.example.invalid/prefix'}):
            self.assertEqual(AOOverview().url, 'https://ao.example.invalid/prefix')
        for url in ['file:///tmp/ao', 'http://user:secret@host', 'http://host?token=secret', 'http://host/#secret']:
            with self.assertRaises(ValueError):
                AOOverview(url)
        for options in [{'timeout': 0}, {'stale_after': -1}, {'timeout': float('nan')}]:
            with self.assertRaises(ValueError):
                AOOverview(**options)


class NormalizationTests(unittest.TestCase):
    def row(self, **changes):
        return dict(deepcopy(FIXTURE['sessions'][0]), **changes)

    def test_identity_encodes_segments_and_does_not_collide(self):
        self.assertEqual(source_ref('ao', 'project/a', 'session ?#'), 'ao:project%2Fa/session%20%3F%23')
        self.assertNotEqual(source_ref('ao', 'a/b', 'c'), source_ref('ao', 'a', 'b/c'))
        row = normalize(self.row(projectId='p/a', id='s#'), NOW, 3600)
        self.assertEqual(row['source']['link'], 'ao://sessions/p%2Fa/s%23')

    def test_silence_delivery_text_and_session_exit_are_not_completion(self):
        for text in ['Message accepted', 'Instruction delivered', 'Task completed']:
            row = normalize(self.row(latestAssistantUpdate=text, status='idle', activity={'state': 'idle'}, lastEventAt=None), NOW, 3600)
            self.assertEqual(row['status'], 'unknown')
        for activity in [{'state': 'exited'}, {'state': 'idle'}]:
            row = normalize(self.row(status='terminated', isTerminated=True, activity=activity), NOW, 3600)
            self.assertEqual(row['status'], 'unknown')

    def test_readiness_and_clock_skew(self):
        for readiness in [None, 'checking', 'unavailable']:
            self.assertEqual(normalize(self.row(statusReadiness=readiness), NOW, 3600)['status'], 'unknown')
        row = self.row(lastEventAt='2099-01-01T00:00:00Z')
        self.assertNotEqual(normalize(row, NOW, 3600)['status'], 'stale')
        for value in ['invalid', None, '0001-01-01T00:00:00Z', '2026-01-01T00:00:00']:
            self.assertIsNone(instant(value))

    def test_unknown_status_is_not_overridden_by_activity(self):
        for raw in ['future_status', None, '']:
            for state in ['active', 'idle', 'waiting_input', 'blocked']:
                for event in ['2026-10-22T10:00:00Z', '2026-10-21T08:00:00Z']:
                    with self.subTest(status=raw, state=state, event=event):
                        row = normalize(self.row(status=raw, activity={'state': state},
                                                 lastEventAt=event), NOW, 3600)
                        self.assertEqual(row['status'], 'unknown')
                        self.assertEqual(row['status_label'], 'неизвестно')
                        self.assertEqual(row['raw_status'], raw)
                        self.assertIsNone(row['blocker'])
                        self.assertIsNone(row['expected_decision'])

    def test_stale_threshold_is_configurable_and_waits_stay_waits(self):
        self.assertEqual(normalize(self.row(), NOW, 300)['status'], 'stale')
        self.assertEqual(normalize(self.row(), NOW, 301)['status'], 'working')
        for row in FIXTURE['sessions'][1:3]:
            self.assertIn(normalize(row, NOW, 1)['status'], {'waiting_user', 'blocked'})


class OverviewToolTests(unittest.TestCase):
    def test_gateway_exposes_only_read_overview_under_request_grant(self):
        import tempfile
        import uuid
        source = AOOverview(clock=lambda: NOW)
        with tempfile.TemporaryDirectory() as root:
            gateway = Gateway(root, Tasks(root, ao_source=source))
            rid = str(uuid.uuid4())
            body = {'messages': [{'role': 'user', 'content': 'Hub context (data):\n{"context":{"id":"fixture"}}'},
                                 {'role': 'user', 'content': 'What are the tasks?'}]}
            gateway.begin(body, rid)
            with patch.object(source, 'read_sessions', return_value=FIXTURE['sessions']):
                self.assertEqual(len(gateway.tool('ao_overview', {'request_id': rid})['tasks']), 6)
            gateway.complete(rid, {})
            with self.assertRaises(ValueError):
                gateway.tool('ao_overview', {'request_id': rid})
        self.assertIn('ao_overview', [t[0] for t in TOOLS])
