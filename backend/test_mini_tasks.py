import json
import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from mini_tasks import Gateway, Tasks, forward_text, timestamp, TOOLS


class NativeFixture:
    def __init__(self):
        self.calls = []
        self.busy = False
        self.rc = 0

    def read(self, engine, task):
        if engine not in {'codex', 'claude', 'orchestrator'} or task != 'known':
            raise ValueError('unknown')
        return {'turns': [{'status': 'inProgress' if self.busy else 'completed'}]}

    def list(self, *args):
        return {'tasks': []}

    def execute(self, *args):
        self.calls.append(args)
        return self.rc


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = NativeFixture()
        self.gateway = Gateway(self.temp.name, self.native)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, prompt, topic='topic-one', request_id=None):
        rid = request_id or str(uuid.uuid4())
        body = {'messages': [{'role': 'user', 'content': 'Hub context (data):\n' + json.dumps({'context': {'id': topic}})}, {'role': 'user', 'content': prompt}]}
        result = self.gateway.begin(body, rid)
        return rid, body, result

    def select(self, topic='topic-one'):
        rid, _, _ = self.request('Выбери задачу known', topic)
        self.gateway.tool('task_select', {'request_id': rid, 'engine': 'codex', 'task_id': 'known'})

    def test_report_cannot_authorize_send(self):
        rid, _, _ = self.request('Прочитай результат задачи')
        self.assertIsNone(self.gateway.forward(rid))
        self.assertNotIn('task_send', [t[0] for t in TOOLS])

    def test_report_cannot_change_selection(self):
        rid, _, _ = self.request('Прочитай результат задачи')
        with self.assertRaises(ValueError):
            self.gateway.tool('task_select', {'request_id': rid, 'engine': 'codex', 'task_id': 'known'})

    def test_exact_human_text_only(self):
        self.select()
        rid, _, _ = self.request('Передай: Исправь тест. Не меняй настройки.\nПокажи diff.')
        self.gateway.forward(rid)
        self.gateway.work_once()
        self.assertEqual(self.native.calls[0], ('codex', 'known', 'Исправь тест. Не меняй настройки.\nПокажи diff.', rid))

    def test_topic_isolation(self):
        self.select()
        rid, _, _ = self.request('Передай: проверка', 'other-topic')
        self.assertIn('Сначала выбери', self.gateway.forward(rid))
        self.gateway.work_once()
        self.assertFalse(self.native.calls)

    def test_ambiguous_no_colon_is_discussion(self):
        for prompt in ['Может передай ему потом', 'Он написал: передай: секрет', 'передай', 'передай:   ']:
            self.assertIsNone(forward_text(prompt))

    def test_retry_returns_cached_without_native_call(self):
        rid, body, _ = self.request('Привет')
        self.gateway.complete(rid, {'reply': 'hello'})
        self.assertEqual(self.gateway.begin(body, rid), {'reply': 'hello'})
        body['messages'][-1]['content'] = 'different'
        with self.assertRaises(ValueError):
            self.gateway.begin(body, rid)

    def test_in_flight_is_not_replayed(self):
        rid, body, _ = self.request('Привет')
        with self.assertRaises(ValueError):
            self.gateway.begin(body, rid)

    def test_expired_and_completed_grants_cannot_use_tools(self):
        rid, _, _ = self.request('Привет')
        with self.gateway.db() as db:
            db.execute('UPDATE requests SET created=? WHERE id=?', (time.time()-601, rid))
        with self.assertRaises(ValueError):
            self.gateway.tool('tasks_list', {'request_id': rid})
        rid, _, _ = self.request('Привет')
        self.gateway.complete(rid, {})
        with self.assertRaises(ValueError):
            self.gateway.tool('tasks_list', {'request_id': rid})

    def test_busy_codex_waits_then_runs_once(self):
        self.select()
        rid, _, _ = self.request('Передай: проверь')
        self.gateway.forward(rid)
        self.native.busy = True
        self.gateway.work_once()
        self.assertFalse(self.native.calls)
        self.native.busy = False
        self.gateway.work_once()
        self.gateway.work_once()
        self.assertEqual(len(self.native.calls), 1)

    def test_uncertain_dispatch_never_retried_after_restart(self):
        self.select()
        rid, _, _ = self.request('Передай: проверь')
        self.gateway.forward(rid)
        with self.gateway.db() as db:
            db.execute("UPDATE commands SET status='dispatched' WHERE id=?", (rid,))
        restarted = Gateway(self.temp.name, self.native)
        restarted.work_once()
        self.assertFalse(self.native.calls)
        with restarted.db() as db:
            self.assertEqual(db.execute('SELECT status FROM commands WHERE id=?', (rid,)).fetchone()[0], 'unknown')

    def test_native_error_not_reported_as_delivered_or_retried(self):
        self.select()
        rid, _, _ = self.request('Передай: проверь')
        self.gateway.forward(rid)
        self.native.rc = 1
        self.gateway.work_once()
        self.gateway.work_once()
        with self.gateway.db() as db:
            self.assertEqual(db.execute('SELECT status FROM commands WHERE id=?', (rid,)).fetchone()[0], 'unknown')
        self.assertEqual(len(self.native.calls), 1)

    def test_state_permissions(self):
        self.assertEqual((Path(self.temp.name) / 'tasks.sqlite').stat().st_mode & 0o777, 0o600)

    def test_context_required(self):
        with self.assertRaises(ValueError):
            self.gateway.begin({'messages': [{'role': 'user', 'content': 'hello'}]}, str(uuid.uuid4()))


class NativeReadTests(unittest.TestCase):
    def test_read_receipts_and_ao_ownership(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / '.codex').mkdir()
            dbpath = home / '.codex/state_5.sqlite'
            with sqlite3.connect(dbpath) as db:
                db.executescript('CREATE TABLE threads(id,title,archived); CREATE TABLE thread_read_receipts(thread_id,first_unread_turn);')
                db.executemany('INSERT INTO threads VALUES(?,?,0)', [('one', 'Named task'), ('managed', 'AO worker'), ('read', 'Read task')])
                db.executemany('INSERT INTO thread_read_receipts VALUES(?,?)', [('one', 'turn'), ('read', None)])
            class Reader:
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def list_chats(self, *_):
                    return {'data': [{'id': i, 'name': None, 'updatedAt': 10} for i in ['one', 'managed', 'read', 'unknown']]}
            tasks = Tasks(home)
            tasks.reader = lambda: Reader()
            tasks.orchestrator = lambda: [{'id': 'ao-1', 'provider_conversation_id': 'managed', 'harness': 'codex',
                'title': 'Orchestrator', 'status': 'idle', 'updated': '2026-10-21 15:58:47.889865 +0000 UTC', 'is_terminated': False}]
            before = dbpath.read_bytes()
            rows = tasks.list('codex', unread_only=True)['tasks']
            self.assertEqual([(r['id'], r['title'], r['unread']) for r in rows], [('one', 'Named task', True)])
            rows = tasks.list('codex')['tasks']
            self.assertIsNone(next(r for r in rows if r['id'] == 'unknown')['unread'])
            self.assertEqual(before, dbpath.read_bytes())

    def test_large_report_bounded_and_tool_results_excluded(self):
        class Reader:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def call(self, name, args):
                if name == 'thread/read': return {'thread': {'name': 'Task', 'cwd': '/fixture'}}
                return {'data': [{'status': 'completed', 'items': [
                    {'type': 'commandExecution', 'output': 'PRIVATE COMMAND OUTPUT'},
                    {'type': 'agentMessage', 'text': 'x' * 90000}]}]}
        tasks = Tasks()
        tasks.reader = lambda: Reader()
        value = tasks.read('codex', str(uuid.uuid4()))
        encoded = json.dumps(value)
        self.assertNotIn('PRIVATE COMMAND OUTPUT', encoded)
        self.assertIn('[truncated]', encoded)
        self.assertLess(len(encoded), 20000)
        self.assertEqual(value['turns'][0]['status'], 'completed')

    def test_timestamp_engines_use_same_ordering(self):
        self.assertEqual(timestamp('2026-10-21 15:58:47.889865 +0000 UTC'), 1792598327)
        self.assertEqual(timestamp(1792598327000), 1792598327)

    def test_claude_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            Tasks().read('claude', '../../secret')


if __name__ == '__main__':
    unittest.main()
