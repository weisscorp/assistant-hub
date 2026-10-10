from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from jobs import JobStore, Conflict, QueueFull, TERMINAL


def identifier():
    return str(uuid.uuid4())


def wait_for(check, timeout=6):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = check()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError('Timed out waiting for observed process/state')


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def store(self, argv=None, **options):
        store = JobStore(argv or [sys.executable, '-c', 'import sys;print(sys.stdin.read())'], self.root / f'state-{len(self.stores)}', **options)
        self.stores.append(store)
        return store

    def terminal(self, store, key):
        return wait_for(lambda: (value if (value := store.get(key))['state'] in TERMINAL else None))

    def test_literal_stdin_idempotency_conflict_and_input_erasure(self):
        store = self.store()
        key = identifier(); text = 'Привет $(no shell); `literal`'
        threads = [threading.Thread(target=store.submit, args=(key, text)) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        result = self.terminal(store, key)
        self.assertEqual('succeeded', result['state'])
        self.assertEqual(text, result['reply'])
        self.assertEqual(result, store.submit(key, text))
        with self.assertRaises(Conflict): store.submit(key, 'different')
        with closing(sqlite3.connect(store.directory / 'jobs.sqlite3')) as db:
            self.assertEqual(('', 1), db.execute('SELECT text,count(*) FROM jobs WHERE id=?', (key,)).fetchone())
        self.assertEqual(0o600, (store.directory / 'jobs.sqlite3').stat().st_mode & 0o777)

    def test_agent_receives_job_id_without_bridge_or_signing_secrets(self):
        key = identifier()
        script = 'import os,json;print(json.dumps({k:os.environ.get(k) for k in ("HELMET_JOB_ID","ASSISTANT_TOKEN","TLS_KEY","KEYSTORE_PASSWORD","KEY_PASSWORD","HELMET_MODEL_API_KEY")}))'
        with patch.dict(os.environ, {'ASSISTANT_TOKEN': 'fixture-token', 'TLS_KEY': 'fixture-key',
                                   'KEYSTORE_PASSWORD': 'fixture-password', 'KEY_PASSWORD': 'fixture-password',
                                   'HELMET_MODEL_API_KEY': 'fixture-model-key'}):
            store = self.store([sys.executable, '-c', script])
            store.submit(key, 'test')
            result = self.terminal(store, key)
        self.assertEqual('succeeded', result['state'])
        values = json.loads(result['reply'])
        self.assertEqual(key, values.pop('HELMET_JOB_ID'))
        self.assertEqual('fixture-model-key', values.pop('HELMET_MODEL_API_KEY'))
        self.assertTrue(all(value is None for value in values.values()))

    def test_fifo_queue_limit_and_cancel_before_delayed_post(self):
        store = self.store([sys.executable, '-c', 'import sys,time;time.sleep(.6);print(sys.stdin.read())'], max_pending=2)
        first, second, third = identifier(), identifier(), identifier()
        store.submit(first, 'one')
        wait_for(lambda: store.get(first)['state'] == 'running')
        store.submit(second, 'two')
        self.assertEqual('queued', store.get(second)['state'])
        with self.assertRaises(QueueFull): store.submit(third, 'three')
        self.assertEqual('cancelled', store.cancel(second)['state'])
        self.assertEqual('cancelled', store.submit(second, 'two')['state'])
        unknown = identifier()
        self.assertEqual('cancelled', store.cancel(unknown)['state'])
        self.assertEqual('cancelled', store.submit(unknown, 'arrived late')['state'])
        self.assertEqual('one', self.terminal(store, first)['reply'])

    def test_timeout_nonzero_and_empty_output_are_not_success(self):
        for argv, state in [([sys.executable, '-c', 'import time;time.sleep(10)'], 'timed_out'),
                            ([sys.executable, '-c', 'print("fake success");raise SystemExit(2)'], 'failed'),
                            ([sys.executable, '-c', 'raise SystemExit(124)'], 'failed'),
                            ([sys.executable, '-c', 'pass'], 'failed')]:
            store = self.store(argv, timeout=.2)
            key = identifier(); store.submit(key, 'test')
            self.assertEqual(state, self.terminal(store, key)['state'])

    def descendant_agent(self):
        heartbeat = self.root / 'heartbeat'
        script = self.root / 'agent.py'
        child = "import signal,time,sys;signal.signal(signal.SIGTERM,signal.SIG_IGN)\nwhile True:\n with open(sys.argv[1],'a') as f:f.write('x');f.flush()\n time.sleep(.03)"
        script.write_text("import subprocess,sys,signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nchild=subprocess.Popen([sys.executable,'-c'," + repr(child) + ",sys.argv[1]])\nwhile True:time.sleep(1)\n")
        return [sys.executable, str(script), str(heartbeat)], heartbeat

    def test_cancellation_stops_signal_ignoring_descendant(self):
        argv, heartbeat = self.descendant_agent()
        store = self.store(argv)
        key = identifier(); store.submit(key, 'cancel me')
        wait_for(lambda: heartbeat.exists() and heartbeat.stat().st_size > 2)
        self.assertEqual('cancelling', store.cancel(key)['state'])
        self.assertEqual('cancelled', self.terminal(store, key)['state'])
        length = heartbeat.stat().st_size
        time.sleep(.25)
        self.assertEqual(length, heartbeat.stat().st_size, 'Agent descendant survived cancellation')

    def test_parent_death_stops_descendants_and_running_job_is_not_replayed(self):
        argv, heartbeat = self.descendant_agent()
        key = identifier(); directory = self.root / 'crash-state'
        code = 'import sys,time,json;from jobs import JobStore;s=JobStore(json.loads(sys.argv[1]),sys.argv[2]);s.submit(sys.argv[3],"do not replay");time.sleep(60)'
        parent = subprocess.Popen([sys.executable, '-c', code, json.dumps(argv), str(directory), key], cwd=Path(__file__).parent)
        try:
            wait_for(lambda: heartbeat.exists() and heartbeat.stat().st_size > 2)
            parent.kill(); parent.wait(timeout=3)
            time.sleep(.4)
            length = heartbeat.stat().st_size; time.sleep(.25)
            self.assertEqual(length, heartbeat.stat().st_size, 'Descendant survived bridge process death')
            recovered = JobStore([sys.executable, '-c', 'import sys;print(sys.stdin.read())'], directory)
            self.stores.append(recovered)
            self.assertEqual('interrupted', recovered.get(key)['state'])
            self.assertEqual('interrupted', recovered.submit(key, 'do not replay')['state'])
            next_id = identifier(); recovered.submit(next_id, 'new job')
            self.assertEqual('new job', self.terminal(recovered, next_id)['reply'])
        finally:
            if parent.poll() is None: parent.kill(); parent.wait(timeout=3)

    def test_shutdown_preserves_queued_job_and_single_owner(self):
        store = self.store([sys.executable, '-c', 'import time;time.sleep(10)'])
        one, two = identifier(), identifier()
        store.submit(one, 'running'); wait_for(lambda: store.get(one)['state'] == 'running')
        store.submit(two, 'queued')
        with self.assertRaises(RuntimeError): JobStore([], store.directory)
        store.close()
        recovered = JobStore([sys.executable, '-c', 'import sys;print(sys.stdin.read())'], store.directory)
        self.stores.append(recovered)
        self.assertEqual('interrupted', recovered.get(one)['state'])
        self.assertEqual('queued', self.terminal(recovered, two)['reply'])


if __name__ == '__main__': unittest.main()
