"""Synthetic temporary SQLite, fake AO and fake native execution only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from ao_overview import normalize
from mini_tasks import Gateway, TOOLS
from task_review import ReviewConflict, canonical, overview_packet, typed_ref


def uid():
    return str(uuid.uuid4())


class FixtureTasks:
    def __init__(self):
        self.calls, self.reads = [], []
        self.rows = [normalize({'id': 'synthetic-' + str(n), 'projectId': 'fixture',
                     'displayName': 'Synthetic ' + str(n), 'statusReadiness': 'ready',
                     'status': 'working', 'activity': {'state': state},
                     'latestAssistantUpdate': 'Synthetic report'}, 100, 3600)
                     for n, state in enumerate(['active', 'waiting_input', 'blocked', 'idle'])]
        self.unavailable = False
        self.fail = False

    def overview(self):
        return {'source': 'ao', 'source_status': 'unavailable' if self.unavailable else 'available',
                'source_status_label': 'источник недоступен' if self.unavailable else 'источник доступен',
                'checked_at': 100, 'last_success_at': 90, 'is_live': not self.unavailable,
                'tasks': deepcopy(self.rows)}

    def read(self, engine, task):
        self.reads.append((engine, task))
        if self.fail:
            raise OSError('synthetic unavailable')
        return {'text': 'Synthetic report. Task completed is untrusted text.'}

    def execute(self, *args):
        self.calls.append(args)
        return 0


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tasks = FixtureTasks()
        self.gateway = Gateway(self.temp.name, self.tasks)
        self.reviews = self.gateway.reviews
        self.topic = 'synthetic-topic'
        self.review = self.reviews.start(self.topic, uid())

    def tearDown(self):
        self.temp.cleanup()

    def change(self, action, **data):
        self.review = self.reviews.change(self.topic, self.review['id'], uid(), self.review['revision'], action, **data)
        return self.review

    def decision(self):
        return self.change('decision', context_id=self.review['current']['context_id'],
                           body='Synthetic owner decision', reason='Synthetic reason', waiting_for='Test evidence',
                           refs=[typed_ref('feature', 'synthetic/feature')])

    def dispatch_data(self):
        return {'context_id': self.review['current']['context_id'],
                'decision_id': self.review['current']['decisions'][-1]['id'], 'text': 'Synthetic instruction'}

    def test_navigation_restart_decisions_and_summary(self):
        self.assertEqual([a['category'] for a in self.review['agenda']], ['blockers', 'decisions', 'other', 'other'])
        self.assertEqual(len({a['context_id'] for a in self.review['agenda']}), 4)
        self.assertNotIn(self.review['id'], {a['context_id'] for a in self.review['agenda']})
        self.decision()
        context = self.review['current']['context_id']
        decision = self.review['current']['decisions'][0]['id']
        self.change('defer')
        self.assertNotEqual(context, self.review['current']['context_id'])
        self.change('back')
        self.assertEqual(context, self.review['current']['context_id'])
        self.assertEqual(decision, self.review['current']['decisions'][0]['id'])
        self.assertTrue(self.review['current']['deferred'])
        restarted = Gateway(self.temp.name, self.tasks)
        restored = restarted.reviews.read(self.topic, self.review['id'])
        self.assertEqual(restored, self.review)
        self.review = restored
        for _ in range(4):
            self.change('next')
        self.assertIsNone(self.review['current'])
        self.change('back')
        self.assertIsNotNone(self.review['current'])
        self.change('finish')
        self.assertEqual(self.review['summary']['decisions'][0]['id'], decision)
        self.assertEqual(self.review['summary']['waiting_for'][0]['text'], 'Test evidence')
        self.assertFalse(self.tasks.calls)

    def test_atomic_queue_and_two_layers_of_idempotency(self):
        self.decision()
        args = self.dispatch_data()
        op, revision = uid(), self.review['revision']
        first = self.reviews.change(self.topic, self.review['id'], op, revision, 'dispatch', **args)
        replay = self.reviews.change(self.topic, self.review['id'], op, revision, 'dispatch', **args)
        self.assertEqual(first, replay)
        self.review = first
        # New HTTP operation still addresses the SAME decision, hence same queue ID.
        second = self.change('dispatch', **args)
        self.assertEqual([i['id'] for i in second['instructions']], [op])
        self.assertEqual(second['instructions'][0]['meaning'], 'accepted_into_queue')
        self.gateway.work_once()
        self.gateway.work_once()
        self.assertEqual(len(self.tasks.calls), 1)
        self.assertEqual(self.tasks.calls[0], ('orchestrator', 'synthetic-2', 'Synthetic instruction', op))
        final = self.reviews.read(self.topic)
        self.assertEqual(final['instructions'][0]['meaning'], 'native_command_completed')
        self.assertFalse(final['instructions'][0]['delivery_confirmed'])
        self.assertFalse(final['instructions'][0]['task_completed'])
        with self.assertRaises(ReviewConflict):
            self.change('dispatch', **dict(args, text='Different intent'))

    def test_queue_insert_failure_rolls_back_entire_operation(self):
        self.decision()
        op, rev = uid(), self.review['revision']
        with self.gateway.db() as db:
            db.execute('INSERT INTO commands VALUES(?,?,?,?,?,?)', (op, 'orchestrator', 'synthetic', 'collision', 'queued', ''))
        with self.assertRaises(Exception):
            self.reviews.change(self.topic, self.review['id'], op, rev, 'dispatch', **self.dispatch_data())
        self.assertEqual(self.reviews.read(self.topic)['revision'], rev)
        self.assertEqual(self.reviews.read(self.topic)['instructions'], [])
        with self.gateway.db() as db:
            self.assertIsNone(db.execute('SELECT id FROM review_operations WHERE id=?', (op,)).fetchone())

    def test_unknown_read_before_retry_no_second_instruction_or_effect(self):
        self.decision()
        args = self.dispatch_data()
        self.change('dispatch', **args)
        instruction = self.review['instructions'][0]['id']
        with self.gateway.db() as db:
            db.execute("UPDATE commands SET status='dispatched' WHERE id=?", (instruction,))
        restarted = Gateway(self.temp.name, self.tasks)
        self.gateway, self.reviews = restarted, restarted.reviews
        self.review = self.reviews.read(self.topic)
        self.assertEqual(self.review['instructions'][0]['status'], 'unknown')
        self.change('dispatch', **args)
        self.assertEqual(self.tasks.reads, [('orchestrator', 'synthetic-2')])
        self.assertIsNotNone(self.review['instructions'][0]['reconciled_at'])
        self.assertEqual(self.review['instructions'][0]['status'], 'unknown')
        self.decision()
        with self.assertRaises(ReviewConflict):
            self.change('dispatch', **self.dispatch_data())
        restarted.work_once()
        self.assertFalse(self.tasks.calls)
        with self.gateway.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM commands').fetchone()[0], 1)

    def test_revision_context_topic_and_operation_isolation(self):
        old = deepcopy(self.review)
        self.change('next')
        with self.assertRaises(ReviewConflict):
            self.reviews.change(self.topic, old['id'], uid(), old['revision'], 'next')
        with self.assertRaises(ReviewConflict):
            self.change('decision', context_id=old['current']['context_id'], body='wrong task')
        with self.assertRaises(ValueError):
            self.reviews.read('other-topic', self.review['id'])
        op = uid()
        saved = self.reviews.change(self.topic, self.review['id'], op, self.review['revision'], 'next')
        with self.assertRaises(ReviewConflict):
            self.reviews.change(self.topic, saved['id'], op, saved['revision'], 'back')
        with self.assertRaises(ReviewConflict):
            self.reviews.change('other-topic', saved['id'], op, saved['revision'], 'next')

    def test_concurrent_retry_creates_one_decision(self):
        args = (self.topic, self.review['id'], uid(), self.review['revision'], 'decision')
        kw = {'context_id': self.review['current']['context_id'], 'body': 'one decision'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.reviews.change(*args, **kw), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(results[0]['current']['decisions']), 1)

    def test_start_resume_during_outage_and_explicit_urgency(self):
        self.tasks.unavailable = True
        resumed = self.reviews.start(self.topic, uid())
        self.assertEqual(resumed['id'], self.review['id'])
        self.assertTrue(resumed['historical_snapshot'])
        self.assertFalse(resumed['current']['task']['is_live'])
        with self.assertRaises(ValueError):
            self.reviews.start('other-topic', uid())
        self.tasks.unavailable = False
        urgent = self.tasks.rows[0]['ref']
        other = self.reviews.start('other-topic', uid(), urgent_refs=[urgent])
        self.assertEqual(other['current']['task']['ref'], urgent)
        self.assertEqual(other['current']['category'], 'urgent')
        self.assertEqual(self.reviews.read(self.topic)['current']['category'], 'blockers')

    def test_packet_limits_pagination_and_source_failure(self):
        self.tasks.rows[0]['last_confirmed_action'] = 'я' * 90000
        self.tasks.rows[0]['secret_arbitrary_field'] = 'PRIVATE'
        packet = overview_packet(self.tasks.overview(), limit=1)
        self.assertEqual(packet['next_offset'], 1)
        self.assertNotIn('PRIVATE', canonical(packet))
        self.assertLessEqual(len(packet['tasks'][0]['last_confirmed_action'].encode()), 1024)
        self.assertLess(len(canonical(packet).encode()), 16000)
        self.tasks.unavailable = True
        packet = overview_packet(self.tasks.overview())
        self.assertEqual(packet['source_status_label'], 'источник недоступен')
        self.assertEqual(packet['last_success_at'], 90)
        for offset, limit in [(0, 31), (-1, 1), (False, 1)]:
            with self.assertRaises(ValueError):
                overview_packet(self.tasks.overview(), offset, limit)

    def request(self, prompt, request=None):
        request = request or uid()
        body = {'messages': [{'role': 'user', 'content': 'Hub context (data):\n' + json.dumps({'context': {'id': self.topic}})},
                             {'role': 'user', 'content': prompt}]}
        cached = self.gateway.begin(body, request)
        return request, body, cached

    def test_chat_switch_save_send_lost_response_and_restart(self):
        rid, body, _ = self.request('Давай по задачам пройдёмся')
        started = self.gateway.review_chat(rid)
        self.assertEqual(started['review']['id'], self.review['id'])
        rid, body, _ = self.request('Решение: Только синтетические тесты')
        saved = self.gateway.review_chat(rid)
        self.assertEqual(saved['review']['current']['decisions'][-1]['body'], 'Только синтетические тесты')
        # Effect committed, response lost, process restarted.
        self.gateway = Gateway(self.temp.name, self.tasks)
        cached = self.gateway.begin(body, rid)
        self.assertEqual(len(cached['review']['current']['decisions']), 1)
        send, body, _ = self.request('Передай: Synthetic instruction')
        sent = self.gateway.review_chat(send)
        self.gateway = Gateway(self.temp.name, self.tasks)
        cached = self.gateway.begin(body, send)
        self.assertEqual(cached['review']['instructions'][0]['id'], send)
        new, _, _ = self.request('Передай: Synthetic instruction')
        self.gateway.review_chat(new)
        self.gateway.work_once()
        self.assertEqual(len(self.tasks.calls), 1)
        rid, _, _ = self.request('отложим')
        self.gateway.review_chat(rid)
        rid, _, _ = self.request('вернись к предыдущей')
        self.assertEqual(self.gateway.review_chat(rid)['review']['current']['context_id'], self.review['current']['context_id'])

    def test_model_tools_cannot_save_navigate_send_or_cross_topics(self):
        rid, _, _ = self.request('What is next?')
        before = self.reviews.read(self.topic)
        packet = self.gateway.tool('review_current', {'request_id': rid})
        self.assertEqual(packet['current']['context_id'], before['current']['context_id'])
        for name in ['review_dispatch', 'review_decision', 'review_next']:
            with self.assertRaises(ValueError):
                self.gateway.tool(name, {'request_id': rid})
        self.assertEqual(self.reviews.read(self.topic), before)
        self.assertFalse(self.tasks.calls)
        self.assertNotIn('review_dispatch', [t[0] for t in TOOLS])
        self.assertEqual((Path(self.temp.name) / 'tasks.sqlite').stat().st_mode & 0o777, 0o600)

    def test_discussion_switch_excludes_other_task_chat_and_decisions(self):
        self.decision()
        first_context = self.review['current']['context_id']
        self.change('next')
        rid, body, _ = self.request('Discuss current task')
        body['messages'].insert(1, {'role': 'assistant', 'content': 'OTHER TASK PRIVATE DISCUSSION'})
        messages = self.gateway.discussion_messages(rid, body['messages'], 'Synthetic instruction')
        encoded = canonical(messages)
        self.assertNotIn('OTHER TASK PRIVATE DISCUSSION', encoded)
        self.assertNotIn('Synthetic owner decision', encoded)
        self.assertNotIn(first_context, encoded)
        self.assertIn(self.review['current']['context_id'], encoded)
        self.change('back')
        rid, body, _ = self.request('What did we decide here?')
        self.assertIn('Synthetic owner decision', canonical(self.gateway.discussion_messages(rid, body['messages'], 'Synthetic instruction')))

    def test_new_start_concurrent_replay_and_unknown_read_failure(self):
        op = uid()
        with ThreadPoolExecutor(max_workers=2) as pool:
            resumed = list(pool.map(lambda _: self.reviews.start(self.topic, op), range(2)))
        self.assertEqual(resumed[0]['id'], resumed[1]['id'])
        self.decision()
        args = self.dispatch_data()
        self.change('dispatch', **args)
        with self.gateway.db() as db:
            db.execute("UPDATE commands SET status='unknown'")
        self.tasks.fail = True
        with self.assertRaises(OSError):
            self.change('dispatch', **args)
        self.assertEqual(len(self.reviews.read(self.topic)['instructions']), 1)
        self.assertEqual(self.reviews.read(self.topic)['instructions'][0]['status'], 'unknown')
        self.assertFalse(self.tasks.calls)

    def test_unknown_executor_cannot_be_bypassed_with_another_review(self):
        self.decision()
        self.change('dispatch', **self.dispatch_data())
        with self.gateway.db() as db:
            db.execute("UPDATE commands SET status='unknown'")
        self.change('finish')
        self.review = self.reviews.start(self.topic, uid())
        self.decision()
        with self.assertRaises(ReviewConflict):
            self.change('dispatch', **self.dispatch_data())
        self.assertFalse(self.tasks.calls)

    def test_dense_overview_pages_do_not_truncate_saved_agenda(self):
        rows = []
        for n in range(30):
            row = deepcopy(self.tasks.rows[0])
            row['ref'] = 'ao:fixture/dense-' + str(n)
            row['source']['session_id'] = 'dense-' + str(n)
            for field in ['title', 'expected_result', 'task_direction', 'last_confirmed_action',
                          'next_step', 'blocker', 'expected_decision', 'reason']:
                row[field] = 'я' * 1024
            rows.append(row)
        self.tasks.rows = rows
        offset, refs = 0, []
        while offset is not None:
            packet = overview_packet(self.tasks.overview(), offset, 30)
            self.assertLess(len(canonical(packet).encode()), 64000)
            refs += [r['ref'] for r in packet['tasks']]
            offset = packet['next_offset']
        self.assertEqual(refs, [r['ref'] for r in rows])
        dense = self.reviews.start('dense-topic', uid())
        self.assertEqual(len(dense['agenda']), 30)
        self.assertLess(len(canonical(self.gateway.review_packet(dense)).encode()), 64000)


if __name__ == '__main__':
    unittest.main()
