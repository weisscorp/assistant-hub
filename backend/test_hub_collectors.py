"""Synthetic source/Hub HTTP, durable retries, plugin deadlines and backpressure."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from hub_collector_plugins import Clock, HttpJson, HttpStatus, pointer
from hub_collectors import Backpressure, Store, deliver, isolated_collect, load_config, plugin_call


def source(**changes):
    return {'id': 'service', 'plugin': 'hub_collector_plugins:Clock', 'options': {},
        'interval_seconds': 60, 'timeout_seconds': 2, 'mode': 'changes', 'title': 'Проверка', **changes}


class HttpFixture:
    def __enter__(self):
        self.requests = []
        self.status = 202
        self.body = {'items': [{'id': 'a', 'value': 1}]}
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                fixture.requests.append(dict(self.headers))
                self.send_response(fixture.status if fixture.status != 202 else 200)
                self.send_header('Location', 'http://127.0.0.1:1/other')
                self.end_headers()
                self.wfile.write(json.dumps(fixture.body).encode())
            def do_POST(self):
                fixture.requests.append({'headers': dict(self.headers), 'body': self.rfile.read(int(self.headers['Content-Length']))})
                self.send_response(fixture.status)
                self.end_headers()
                self.wfile.write(json.dumps({'id': self.headers['Idempotency-Key']}).encode())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        return self
    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.source = source()
        self.store.register(self.source)

    def batch(self, data=1, cursor='next'):
        return {'events': [{'key': 'snapshot', 'kind': 'snapshot', 'data': data}], 'cursor': cursor}

    def finish(self, now=1000, data=1, maximum=100):
        claim = self.store.claim_source(self.source, now)
        return self.store.finish_source(self.source, claim, self.batch(data), now, maximum)

    def test_schedule_claim_is_exclusive_and_recovers_after_deadline(self):
        first = self.store.claim_source(self.source, 1000)
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.claim_source(self.source, 1001))
        later = self.store.claim_source(self.source, 1033)
        self.assertNotEqual(first['lease'], later['lease'])
        self.assertFalse(self.store.finish_source(self.source, first, self.batch(), 1034, 100))

    def test_cursor_and_outbox_survive_restart_with_unchanged_deduplication(self):
        self.finish()
        restarted = Store(self.tmp.name)
        self.assertIsNone(restarted.claim_source(self.source, 1059))
        claim = restarted.claim_source(self.source, 1060)
        self.assertEqual(claim['cursor'], 'next')
        restarted.finish_source(self.source, claim, self.batch(), 1060, 100)
        self.assertEqual(restarted.snapshot()['outbox'], {'pending': 1})
        self.finish(1120, data=2)
        self.assertEqual(self.store.snapshot()['outbox'], {'pending': 2})

    def test_queue_backpressure_does_not_advance_cursor_or_fingerprint(self):
        self.finish(maximum=1)
        claim = self.store.claim_source(self.source, 1060)
        with self.assertRaises(Backpressure):
            self.store.finish_source(self.source, claim, self.batch(2, 'later'), 1060, 1)
        with self.store.connect() as db:
            row = db.execute('SELECT cursor,lease FROM sources').fetchone()
            self.assertEqual(json.loads(row['cursor']), 'next')
            self.assertEqual(row['lease'], claim['lease'])
        delivery = self.store.claim_delivery(1061)
        self.store.finish_delivery(delivery, 'delivered', '', 1061)
        self.store.finish_source(self.source, claim, self.batch(2, 'later'), 1062, 1)
        self.assertEqual(self.store.snapshot()['outbox'], {'delivered': 1, 'pending': 1})

    def test_delivery_reuses_uuid_and_payload_after_crash_or_retry(self):
        self.finish()
        first = self.store.claim_delivery(1000)
        self.assertIsNone(self.store.claim_delivery(1001))
        second = self.store.claim_delivery(1046)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['payload'], second['payload'])
        self.store.finish_delivery(first, 'delivered', '', 1047)
        self.assertEqual(self.store.snapshot()['outbox'], {'sending': 1})
        self.store.finish_delivery(second, 'pending', 'http_503', 1048)
        self.assertIsNone(self.store.claim_delivery(1049))
        third = self.store.claim_delivery(1052)
        self.assertEqual(first['id'], third['id'])
        self.store.finish_delivery(third, 'delivered', '', 1053)
        self.assertEqual(self.store.snapshot()['outbox'], {'delivered': 1})

    def test_changed_configuration_invalidates_running_claim_and_cursor(self):
        old = self.store.claim_source(self.source, 1000)
        updated = source(options={'period_seconds': 10})
        self.store.register(updated)
        self.assertFalse(self.store.finish_source(self.source, old, self.batch(), 1001, 100))
        self.assertIsNone(self.store.claim_source(updated, 1001)['cursor'])

    def test_failed_collection_preserves_cursor_and_records_safe_error_type(self):
        claim = self.store.claim_source(self.source, 1000)
        self.store.fail_source(self.source, claim, 'TimeoutExpired', 1001)
        row = self.store.snapshot()['sources'][0]
        self.assertEqual((row['failures'], row['last_error']), (1, 'TimeoutExpired'))
        self.assertIsNone(self.store.claim_source(self.source, 1060))
        self.assertIsNotNone(self.store.claim_source(self.source, 1061))

    def test_retention_only_removes_delivered_and_dead_can_be_requeued(self):
        self.finish()
        delivery = self.store.claim_delivery(1000)
        self.store.finish_delivery(delivery, 'dead', 'http_403', 1000)
        self.store.prune(1000000, 1)
        self.assertEqual(self.store.snapshot()['outbox'], {'dead': 1})
        self.assertEqual(self.store.retry_dead(), 1)
        again = self.store.claim_delivery(1000001)
        self.assertEqual(again['id'], delivery['id'])
        self.store.finish_delivery(again, 'delivered', '', 1000001)
        self.store.prune(2000000, 1)
        self.assertEqual(self.store.snapshot()['outbox'], {})

    def test_hub_ack_and_backpressure_keep_exact_original_message(self):
        self.finish()
        row = self.store.claim_delivery(1000)
        with HttpFixture() as fixture, patch.dict(os.environ, {'TEST_COLLECTOR_TOKEN': 'synthetic-token'}):
            hub = {'url': fixture.url, 'token_env': 'TEST_COLLECTOR_TOKEN'}
            self.assertEqual(deliver(hub, row), ('delivered', ''))
            self.assertEqual(json.loads(fixture.requests[0]['body'])['data'], 1)
            self.assertEqual(fixture.requests[0]['headers']['Idempotency-Key'], row['id'])
            for status in (409, 429, 503):
                fixture.status = status
                self.assertEqual(deliver(hub, row), ('pending', 'http_' + str(status)))
            for status in (400, 401, 403, 413):
                fixture.status = status
                self.assertEqual(deliver(hub, row), ('dead', 'http_' + str(status)))
            self.assertTrue(all(r['body'] == row['payload'].encode() for r in fixture.requests))

    def test_clock_uses_wall_clock_cursor_and_plugin_process(self):
        result = isolated_collect(self.source, None, 3601)
        self.assertEqual(result['events'][0]['data']['bucket'], 1)
        self.assertEqual(isolated_collect(self.source, result['cursor'], 3650)['events'], [])
        self.assertEqual(isolated_collect(self.source, result['cursor'], 7200)['cursor'], 2)

    def test_custom_mounted_class_and_hard_timeout(self):
        plugin = Path(self.tmp.name) / 'fixture_plugin.py'
        plugin.write_text('from hub_collector_plugins import Batch, Observation\nimport time\n'
            'class Custom:\n def __init__(self, options): self.options=options\n'
            ' def collect(self, *, cursor, now): return Batch([Observation({"answer":42})], "cursor")\n'
            'class Slow(Custom):\n def collect(self, **kw): time.sleep(20)\n')
        with patch.dict(os.environ, {'PYTHONPATH': self.tmp.name + os.pathsep + str(Path(__file__).parent)}):
            result = isolated_collect(source(plugin='fixture_plugin:Custom'), None, 1000)
            self.assertEqual(result['events'][0]['data'], {'answer': 42})
            with self.assertRaises(subprocess.TimeoutExpired):
                isolated_collect(source(plugin='fixture_plugin:Slow', timeout_seconds=1), None, 1000)

    def test_http_selection_items_status_and_redirects(self):
        now = datetime.now(timezone.utc)
        with HttpFixture() as fixture:
            plugin = HttpJson({'url': fixture.url, 'pointer': '/items', 'items': True})
            result = plugin.collect(cursor=None, now=now)
            self.assertEqual(result.events[0].key, 'a')
            fixture.status = 503
            self.assertFalse(HttpStatus({'url': fixture.url}).collect(cursor=None, now=now).events[0].data['healthy'])
            fixture.status = 302
            with self.assertRaises(Exception):
                plugin.collect(cursor=None, now=now)
            self.assertEqual(len(fixture.requests), 3)  # No request followed the redirect.

    def test_config_rejects_duplicates_and_payload_cannot_choose_plugin(self):
        path = Path(self.tmp.name) / 'settings.json'
        good = {'hub': {'url': 'http://hub:8778', 'token_env': 'TOKEN'}, 'sources': [source()]}
        path.write_text(json.dumps(good))
        self.assertEqual(load_config(path)['sources'][0]['id'], 'service')
        bad = [dict(good, sources=[source(), source()]), dict(good, sources=[source(plugin='eval(x)')]),
               dict(good, sources=[source(interval_seconds=True)]), dict(good, extra='anything'),
               dict(good, hub={'url': 'http://hub:8778?token=x', 'token_env': 'TOKEN'})]
        for config in bad:
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_json_pointer_escaping(self):
        self.assertEqual(pointer({'a/b': {'c~d': [42]}}, '/a~1b/c~0d/0'), 42)


if __name__ == '__main__':
    unittest.main()
