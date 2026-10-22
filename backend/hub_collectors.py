"""Scheduled plugins -> durable local outbox -> existing Hub webhook API.

The scheduler and delivery worker are separate threads. A plugin runs in its own
short-lived process with a hard deadline. SQLite is for one host/local volume;
this is not a distributed broker or an agent permission boundary.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid

from hub_collector_plugins import Batch, Observation, endpoint, opener, secret


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Invalid integer setting')
    return value


def load_config(path):
    raw = Path(path).read_bytes()
    if len(raw) > 262144:
        raise ValueError('Collector configuration is too large')
    config = json.loads(raw) if Path(path).suffix == '.json' else tomllib.loads(raw.decode())
    if not isinstance(config, dict) or set(config) - {'hub', 'runtime', 'sources'}:
        raise ValueError('Unknown collector configuration fields')
    hub = config.get('hub', {})
    if not isinstance(hub, dict) or set(hub) - {'url', 'token_env', 'token_file', 'targets'}:
        raise ValueError('Invalid Hub settings')
    url = endpoint(hub['url']).rstrip('/')
    if urllib.parse.urlsplit(url).query or urllib.parse.urlsplit(url).path:
        raise ValueError('Hub URL must be an origin')
    hub['url'] = url
    if bool(hub.get('token_file')) == bool(hub.get('token_env')):
        raise ValueError('Configure exactly one Hub token reference')
    if hub.get('targets', 'auto') not in {'auto', 'phone', 'desktop', 'both'}:
        raise ValueError('Invalid notification targets')
    runtime = config.setdefault('runtime', {})
    if not isinstance(runtime, dict) or set(runtime) - {'state', 'workers', 'max_pending', 'retention_days'}:
        raise ValueError('Invalid runtime settings')
    integer(runtime.setdefault('workers', 4), 1, 32)
    integer(runtime.setdefault('max_pending', 1000), 1, 10000)
    integer(runtime.setdefault('retention_days', 7), 1, 365)
    sources = config.get('sources', [])
    if not isinstance(sources, list) or len(sources) > 128:
        raise ValueError('Configure at most 128 sources')
    ids = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) - {'id', 'plugin', 'interval_seconds', 'timeout_seconds', 'mode', 'title', 'options'}:
            raise ValueError('Unknown source settings')
        name = source.get('id', '')
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}', name) or name in ids:
            raise ValueError('Invalid or duplicate source ID')
        ids.add(name)
        if not isinstance(source.get('plugin'), str) or not re.fullmatch(r'[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*', source['plugin']):
            raise ValueError('Plugin must be module:Class')
        integer(source.setdefault('interval_seconds', 60), 1, 604800)
        integer(source.setdefault('timeout_seconds', 20), 1, 300)
        if source.setdefault('mode', 'changes') not in {'changes', 'always'}:
            raise ValueError('mode must be changes or always')
        title = source.setdefault('title', name)
        if not isinstance(title, str) or not 1 <= len(title) <= 160 or any(ord(c) < 32 for c in title):
            raise ValueError('Invalid source title')
        if not isinstance(source.setdefault('options', {}), dict):
            raise ValueError('Plugin options must be an object')
    return config


def plugin_call(source, cursor, now):
    module, name = source['plugin'].split(':')
    plugin = getattr(importlib.import_module(module), name)(source['options'])
    batch = plugin.collect(cursor=cursor, now=datetime.fromtimestamp(now, timezone.utc))
    if not isinstance(batch, Batch) or not isinstance(batch.events, list) or len(batch.events) > 256:
        raise ValueError('Plugin must return a Batch of at most 256 observations')
    events, keys = [], set()
    for event in batch.events:
        if (not isinstance(event, Observation) or not isinstance(event.key, str) or not 1 <= len(event.key) <= 256
                or event.key in keys or not isinstance(event.kind, str) or not 1 <= len(event.kind) <= 80):
            raise ValueError('Invalid or duplicate observation key')
        keys.add(event.key)
        value = {'key': event.key, 'kind': event.kind, 'data': event.data}
        if len(encode(value).encode()) > 60000:
            raise ValueError('Observation is too large')
        events.append(value)
    result = {'events': events, 'cursor': batch.cursor}
    if len(encode(batch.cursor).encode()) > 32768 or len(encode(result).encode()) > 1048576:
        raise ValueError('Plugin batch/cursor is too large')
    return result


def isolated_collect(source, cursor, now):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), 'plugin'],
        input=encode({'source': source, 'cursor': cursor, 'now': now}), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=source['timeout_seconds'], check=False)
    if result.returncode or len(result.stdout.encode()) > 1048576:
        raise ValueError('Plugin failed')
    return json.loads(result.stdout)


class Backpressure(Exception):
    pass


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / 'collectors.sqlite'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,digest TEXT NOT NULL,cursor TEXT NOT NULL DEFAULT 'null',
                    next_at REAL NOT NULL DEFAULT 0,lease TEXT,lease_until REAL NOT NULL DEFAULT 0,
                    failures INTEGER NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '',last_success REAL);
                CREATE TABLE IF NOT EXISTS fingerprints(source TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,
                    PRIMARY KEY(source,key));
                CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,source TEXT NOT NULL,title TEXT NOT NULL,
                    payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0,lease TEXT,lease_until REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,finished_at REAL);
                CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(status,next_at);
            ''')

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, source):
        digest = hashlib.sha256(encode(source).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM sources WHERE id=?', (source['id'],)).fetchone()
            if old is None:
                db.execute('INSERT INTO sources(id,digest) VALUES(?,?)', (source['id'], digest))
            elif old['digest'] != digest:
                db.execute("UPDATE sources SET digest=?,cursor='null',next_at=0,lease=NULL,lease_until=0,failures=0,last_error='' WHERE id=?", (digest, source['id']))
                db.execute('DELETE FROM fingerprints WHERE source=?', (source['id'],))

    def claim_source(self, source, now):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM sources WHERE id=? AND next_at<=? AND lease_until<=?', (source['id'], now, now)).fetchone()
            if row is None:
                return None
            lease = str(uuid.uuid4())
            db.execute('UPDATE sources SET lease=?,lease_until=? WHERE id=?', (lease, now + source['timeout_seconds'] + 30, source['id']))
            return {'lease': lease, 'cursor': json.loads(row['cursor'])}

    def finish_source(self, source, claim, batch, now, maximum):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM sources WHERE id=? AND lease=?', (source['id'], claim['lease'])).fetchone()
            if row is None:
                return False
            pending = db.execute("SELECT count(*) FROM outbox WHERE status IN ('pending','sending')").fetchone()[0]
            inserts = []
            for event in batch['events']:
                digest = hashlib.sha256(encode([event['kind'], event['data']]).encode()).hexdigest()
                old = db.execute('SELECT digest FROM fingerprints WHERE source=? AND key=?', (source['id'], event['key'])).fetchone()
                if source['mode'] == 'changes' and old and old['digest'] == digest:
                    continue
                event_id = str(uuid.uuid4())
                payload = encode({'version': 1, 'id': event_id, 'source': source['id'],
                    'kind': event['kind'], 'key': event['key'], 'observed_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    'data': event['data']})
                if len(payload.encode()) > 65536:
                    raise ValueError('Wrapped event is too large')
                inserts.append((event_id, payload, event['key'], digest))
            if pending + len(inserts) > maximum:
                raise Backpressure('Outbox is full')
            for event_id, payload, key, digest in inserts:
                db.execute('INSERT INTO outbox(id,source,title,payload,created_at) VALUES(?,?,?,?,?)', (event_id, source['id'], source['title'], payload, now))
                db.execute('INSERT INTO fingerprints VALUES(?,?,?) ON CONFLICT(source,key) DO UPDATE SET digest=excluded.digest', (source['id'], key, digest))
            db.execute("UPDATE sources SET cursor=?,next_at=?,lease=NULL,lease_until=0,failures=0,last_error='',last_success=? WHERE id=?",
                       (encode(batch['cursor']), now + source['interval_seconds'], now, source['id']))
        return True

    def fail_source(self, source, claim, error, now):
        with self.connect() as db:
            row = db.execute('SELECT failures FROM sources WHERE id=? AND lease=?', (source['id'], claim['lease'])).fetchone()
            if row:
                delay = min(3600, max(source['interval_seconds'], 2 ** min(row['failures'] + 1, 11)))
                db.execute('UPDATE sources SET failures=failures+1,last_error=?,next_at=?,lease=NULL,lease_until=0 WHERE id=? AND lease=?',
                           (error, now + delay, source['id'], claim['lease']))

    def claim_delivery(self, now):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM outbox WHERE status IN ('pending','sending') AND next_at<=? AND lease_until<=? ORDER BY created_at,id LIMIT 1", (now, now)).fetchone()
            if row is None:
                return None
            lease = str(uuid.uuid4())
            db.execute("UPDATE outbox SET status='sending',lease=?,lease_until=?,attempts=attempts+1 WHERE id=?", (lease, now + 45, row['id']))
            return {**dict(row), 'lease': lease, 'attempts': row['attempts'] + 1}

    def finish_delivery(self, row, outcome, error, now):
        delay = min(3600, 2 ** min(row['attempts'], 11))
        with self.connect() as db:
            db.execute('UPDATE outbox SET status=?,last_error=?,next_at=?,lease=NULL,lease_until=0,finished_at=? WHERE id=? AND lease=?',
                (outcome, error, now + delay, now if outcome in {'delivered', 'dead'} else None, row['id'], row['lease']))

    def prune(self, now, days):
        with self.connect() as db:
            # Failed events remain available until explicitly retried by an owner.
            db.execute("DELETE FROM outbox WHERE status='delivered' AND finished_at<?", (now - days * 86400,))

    def snapshot(self):
        with self.connect() as db:
            return {'sources': [dict(r) for r in db.execute('SELECT id,next_at,failures,last_error,last_success FROM sources ORDER BY id')],
                'outbox': {r['status']: r['n'] for r in db.execute('SELECT status,count(*) AS n FROM outbox GROUP BY status')}}

    def retry_dead(self):
        with self.connect() as db:
            return db.execute("UPDATE outbox SET status='pending',next_at=0,last_error='',finished_at=NULL WHERE status='dead'").rowcount


def deliver(hub, row):
    token = secret(hub)
    request = urllib.request.Request(hub['url'] + '/webhooks/events', data=row['payload'].encode(), headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
        'Idempotency-Key': row['id'], 'X-Event-Source': row['source'],
        'X-Event-Title': urllib.parse.quote(row['title'], safe=''), 'X-Event-Targets': hub.get('targets', 'auto')})
    try:
        with opener().open(request, timeout=15) as reply:
            body = json.loads(reply.read(8192))
            if reply.status != 202 or body.get('id') != row['id']:
                return 'pending', 'invalid_hub_acknowledgement'
        return 'delivered', ''
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        # Existing Hub uses 409 for both queue/rate backpressure and ID conflicts.
        # Retain the original UUID/payload on retry, never manufacture a new event.
        return ('pending' if code in {408, 409, 425, 429} or code >= 500 else 'dead'), 'http_' + str(code)
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return 'pending', 'hub_unavailable_or_invalid_reply'


class Runner:
    def __init__(self, config):
        self.config = config
        self.store = Store(config['runtime'].get('state', '/data/collectors'))
        self.stop = threading.Event()
        for source in config['sources']:
            self.store.register(source)

    def collect(self, source):
        now = time.time()
        claim = self.store.claim_source(source, now)
        if claim is None:
            return
        try:
            batch = isolated_collect(source, claim['cursor'], now)
            self.store.finish_source(source, claim, batch, time.time(), self.config['runtime']['max_pending'])
        except Exception as error:
            # Exception text can contain source URLs, credentials or collected data.
            name = type(error).__name__
            self.store.fail_source(source, claim, name, time.time())
            print(encode({'source': source['id'], 'status': 'collection_failed', 'error': name}), flush=True)

    def delivery(self, once=False):
        while not self.stop.is_set():
            row = self.store.claim_delivery(time.time())
            if row is None:
                if once:
                    break
                self.stop.wait(1)
                continue
            outcome, error = deliver(self.config['hub'], row)
            self.store.finish_delivery(row, outcome, error, time.time())
            if outcome != 'delivered':
                print(encode({'event_id': row['id'], 'status': outcome, 'error': error}), flush=True)
            # Producer limit is 30/minute in the existing Hub.
            if not once:
                self.stop.wait(2.1)

    def run(self, once=False):
        if once:
            for source in self.config['sources']:
                self.collect(source)
            self.delivery(once=True)
            return
        delivery = threading.Thread(target=self.delivery, name='collector-delivery', daemon=True)
        delivery.start()
        active = {}
        try:
            with ThreadPoolExecutor(max_workers=self.config['runtime']['workers']) as pool:
                while not self.stop.is_set():
                    for source in self.config['sources']:
                        task = active.get(source['id'])
                        if task is None or task.done():
                            active[source['id']] = pool.submit(self.collect, source)
                    self.store.prune(time.time(), self.config['runtime']['retention_days'])
                    self.stop.wait(1)
        finally:
            self.stop.set()
            delivery.join(20)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'once', 'status', 'retry-dead', 'check', 'plugin'])
    parser.add_argument('--config')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == 'plugin':
            packet = json.loads(sys.stdin.read(1048577))
            with contextlib.redirect_stdout(sys.stderr):
                result = plugin_call(packet['source'], packet['cursor'], packet['now'])
            print(encode(result))
            return 0
        if not args.config:
            raise ValueError('Provide --config')
        config = load_config(args.config)
        if args.command == 'check':
            print(encode({'valid': True, 'sources': [s['id'] for s in config['sources']]}))
            return 0
        runner = Runner(config)
        if args.command == 'status':
            print(encode(runner.store.snapshot()))
        elif args.command == 'retry-dead':
            print(encode({'requeued': runner.store.retry_dead()}))
        else:
            signal.signal(signal.SIGTERM, lambda *_: runner.stop.set())
            signal.signal(signal.SIGINT, lambda *_: runner.stop.set())
            runner.run(once=args.command == 'once')
        return 0
    except Exception as error:
        print(encode({'status': 'error', 'error': type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
