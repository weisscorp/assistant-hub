"""Shared node capabilities, acknowledged calls and durable Hub timers."""
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from agent_hub import Conflict, encoded, number, text, uid

TERMINAL = {'succeeded', 'failed', 'cancelled', 'unknown'}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9._-]{0,63}', value):
        raise ValueError('Invalid node/capability identifier')
    return value


def validate_schema(schema, depth=0):
    if not isinstance(schema, dict) or depth > 4:
        raise ValueError('Invalid capability schema')
    allowed = {'type', 'properties', 'required', 'additionalProperties', 'enum',
               'minimum', 'maximum', 'maxLength', 'description'}
    if set(schema) - allowed or schema.get('type') not in {'object', 'string', 'integer', 'boolean'}:
        raise ValueError('Unsupported capability schema keyword/type')
    if schema['type'] == 'object':
        properties, required = schema.get('properties', {}), schema.get('required', [])
        if (schema.get('additionalProperties') is not False or not isinstance(properties, dict)
                or len(properties) > 16 or not isinstance(required, list)
                or any(not isinstance(k, str) or k not in properties for k in required)):
            raise ValueError('Capability objects require closed properties')
        for key, child in properties.items():
            identifier(key)
            validate_schema(child, depth + 1)
    for key in ('minimum', 'maximum', 'maxLength'):
        if key in schema and (type(schema[key]) is not int or abs(schema[key]) > 2**53-1):
            raise ValueError('Invalid schema bound')
    if schema.get('minimum', -2**53+1) > schema.get('maximum', 2**53-1) or schema.get('maxLength', 0) < 0:
        raise ValueError('Invalid schema bound order')
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not 1 <= len(schema['enum']) <= 32):
        raise ValueError('Invalid schema enum')
    if 'description' in schema:
        text(schema['description'], 800, empty=True)
    if 'enum' in schema:
        plain = {k:v for k,v in schema.items() if k != 'enum'}
        for item in schema['enum']:
            if schema['type'] == 'object':
                raise ValueError('Object enums are unsupported')
            validate_arguments(item, plain)


def validate_arguments(value, schema):
    kind = schema['type']
    if kind == 'object':
        properties = schema.get('properties', {})
        if not isinstance(value, dict) or set(value) - set(properties) or set(schema.get('required', [])) - set(value):
            raise ValueError('Invalid capability arguments')
        for key, item in value.items():
            validate_arguments(item, properties[key])
    elif kind == 'string':
        text(value, schema.get('maxLength', 8000), empty=True)
    elif kind == 'integer':
        number(value, schema.get('minimum', -2**53+1), schema.get('maximum', 2**53-1))
    elif type(value) is not bool:
        raise ValueError('Expected boolean')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('Invalid capability enum')


def object_schema(properties=None, required=None):
    return {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}


TIMERS = [
    {'name': 'timer.create', 'description': 'Create a persistent timer in Hub. Fires a topic notification, not a device alarm. Ask for duration if missing.',
     'inputSchema': object_schema({'duration_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 604800},
                                   'label': {'type': 'string', 'maxLength': 160}}, ['duration_seconds'])},
    {'name': 'timer.list', 'description': 'List current-topic timers with status and remaining seconds.', 'inputSchema': object_schema()},
    {'name': 'timer.cancel', 'description': 'Cancel a current-topic timer by its exact ID.',
     'inputSchema': object_schema({'timer_id': {'type': 'string', 'maxLength': 36}}, ['timer_id'])},
]


class Fabric:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        self.closed = threading.Event()
        self.thread = None
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL,
                    capabilities TEXT NOT NULL, seen_at REAL NOT NULL, ttl INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS capability_calls(id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
                    context_id TEXT NOT NULL REFERENCES contexts(id), client TEXT NOT NULL, task_id TEXT,
                    capability TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT 'null', error TEXT NOT NULL DEFAULT '', lease TEXT,
                    deadline REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS node_call_queue ON capability_calls(node_id,status,created_at);
                CREATE INDEX IF NOT EXISTS call_deadlines ON capability_calls(status,deadline);
                CREATE TABLE IF NOT EXISTS timers(id TEXT PRIMARY KEY, context_id TEXT NOT NULL REFERENCES contexts(id),
                    call_id TEXT NOT NULL UNIQUE REFERENCES capability_calls(id), label TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL, due_at REAL NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS due_timers ON timers(status,due_at);
            ''')
            now = self.clock()
            # An executor may already have acted. Never replay a claimed command.
            db.execute("UPDATE capability_calls SET status='unknown',error='Hub restarted after dispatch; outcome unknown',updated_at=? WHERE status='running'", (now,))
            db.execute('INSERT INTO nodes VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET capabilities=excluded.capabilities,seen_at=excluded.seen_at',
                       ('hub', 'Assistant Hub', 'hub', encoded(TIMERS), now, 0))

    def start(self):
        if self.thread is not None:
            return
        def run():
            while not self.closed.wait(0.5):
                try:
                    self.tick()
                except (OSError, ValueError, sqlite3.Error):
                    pass
        self.thread = threading.Thread(target=run, name='hub-timers', daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=2)

    def register(self, node_id, label, kind, capabilities, ttl=90):
        identifier(node_id); text(label, 160); text(kind, 40); number(ttl, 10, 300)
        if node_id == 'hub' or not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 32:
            raise ValueError('Invalid node registration')
        if len(encoded(capabilities).encode()) > 48000:
            raise ValueError('Capability catalog exceeds budget')
        names = set()
        for capability in capabilities:
            if not isinstance(capability, dict) or set(capability) != {'name', 'description', 'inputSchema'}:
                raise ValueError('Invalid capability descriptor')
            name = identifier(capability['name']); text(capability['description'], 800)
            if len(encoded(capability).encode()) > 6000:
                raise ValueError('Capability descriptor exceeds budget')
            if name in names or not isinstance(capability['inputSchema'],dict) or capability['inputSchema'].get('type') != 'object':
                raise ValueError('Duplicate/non-object capability')
            validate_schema(capability['inputSchema']); names.add(name)
        with self.store.connect() as db:
            db.execute('INSERT INTO nodes VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,kind=excluded.kind,capabilities=excluded.capabilities,seen_at=excluded.seen_at,ttl=excluded.ttl',
                       (node_id, label, kind, encoded(capabilities), self.clock(), ttl))
        return {'id': node_id, 'registered': True}

    def nodes(self, query='', schemas=False):
        text(query, 80, empty=True)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM nodes ORDER BY id='hub' DESC,id LIMIT 100").fetchall()
        result, budget = [], 14000
        for row in rows:
            capabilities = json.loads(row['capabilities'])
            identity = ' '.join((row['id'], row['label'], row['kind'])).casefold()
            if query and query.casefold() not in identity:
                capabilities = [c for c in capabilities if query.casefold() in (c['name']+' '+c['description']).casefold()]
            if not capabilities:
                continue
            item = {'id': row['id'], 'label': row['label'], 'kind': row['kind'],
                           'online': row['id'] == 'hub' or self.clock() - row['seen_at'] <= row['ttl'],
                           'capabilities': [] if schemas else [c['name'] for c in capabilities]}
            if schemas:
                budget -= len(encoded(item).encode()) + 100
                for capability in capabilities:
                    size = len(encoded(capability).encode()) + 2
                    if size > budget:
                        item['truncated'] = True
                        break
                    item['capabilities'].append(capability); budget -= size
                if not item['capabilities']:
                    break
            result.append(item)
            if len(result) >= (8 if schemas else 100):
                break
        return result

    def call(self, node, capability, arguments, context, client, call_id, task_id=None):
        identifier(node); identifier(capability); uid(call_id); uid(context); text(client, 128)
        if not isinstance(arguments, dict) or len(encoded(arguments).encode()) > 8000:
            raise ValueError('Capability input exceeds budget')
        if task_id is not None:
            uid(task_id)
        self.tick()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE'); self.store.require_context(db, context)
            existing = db.execute('SELECT * FROM capability_calls WHERE id=?', (call_id,)).fetchone()
            if existing:
                if (existing['node_id'], existing['capability'], json.loads(existing['arguments']), existing['context_id'], existing['client']) != (node, capability, arguments, context, client):
                    raise Conflict('Call ID belongs to a different action')
                return self._call(existing)
            target = db.execute('SELECT * FROM nodes WHERE id=?', (node,)).fetchone()
            if target is None:
                raise ValueError('Unknown node; discover available capabilities first')
            definition = next((c for c in json.loads(target['capabilities']) if c['name'] == capability), None)
            if definition is None:
                raise ValueError('Node does not advertise this capability')
            validate_arguments(arguments, definition['inputSchema'])
            now = self.clock()
            db.execute('INSERT INTO capability_calls(id,node_id,context_id,client,task_id,capability,arguments,status,deadline,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (call_id, node, context, client, task_id, capability, encoded(arguments), 'queued', now+300, now, now))
            self.store._event(db, 'capability.queued', context, task_id, {'id': call_id, 'node': node, 'capability': capability})
        if node == 'hub':
            try:
                result = self._local(capability, arguments, context, call_id)
                status, error = 'succeeded', ''
            except ValueError as exc:
                result, status, error = None, 'failed', str(exc)
            with self.store.connect() as db:
                db.execute('UPDATE capability_calls SET status=?,result=?,error=?,updated_at=? WHERE id=?',
                           (status, encoded(result), error, self.clock(), call_id))
                self.store._event(db, 'capability.'+status, context, task_id, {'id': call_id, 'node': node, 'capability': capability})
        return self.result(call_id, context)

    def hints(self, prompt):
        """Small live names-only preview; fetch schemas only for selected nodes."""
        needle = prompt.casefold()
        with self.store.connect() as db:
            rows = db.execute('SELECT id,label,seen_at,ttl FROM nodes ORDER BY id LIMIT 100').fetchall()
            rows = sorted(rows, key=lambda r: (
                not (r['id'].casefold() in needle or r['label'].casefold() in needle),
                r['id'] != 'hub', -r['seen_at']))
            result = []
            for row in rows[:8]:
                raw = db.execute('SELECT capabilities FROM nodes WHERE id=?', (row['id'],)).fetchone()[0]
                capabilities = json.loads(raw)
                item = {'id':row['id'], 'label':row['label'],
                        'online':row['id']=='hub' or self.clock()-row['seen_at']<=row['ttl'],
                        'capabilities':[c['name'] for c in capabilities[:6]],
                        'more_capabilities':len(capabilities)>6}
                if len(encoded(result+[item]).encode()) > 1500:
                    break
                result.append(item)
        return result

    @staticmethod
    def _call(row):
        value = dict(row)
        for key in ('arguments', 'result'):
            value[key] = json.loads(value[key])
        return value

    def result(self, call_id, context=None):
        self.tick()
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM capability_calls WHERE id=?', (uid(call_id),)).fetchone()
        if row is None or context is not None and row['context_id'] != context:
            raise ValueError('Unknown call in this topic')
        return self._call(row)

    def calls(self, context):
        with self.store.connect() as db:
            return [self._call(r) for r in db.execute('SELECT * FROM capability_calls WHERE context_id=? ORDER BY created_at DESC LIMIT 30', (context,))]

    def cancel_task(self, task_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT * FROM capability_calls WHERE task_id=? AND status='queued'", (task_id,)).fetchall():
                db.execute("UPDATE capability_calls SET status='cancelled',error='Parent task cancelled before dispatch',updated_at=? WHERE id=?", (self.clock(),row['id']))
                self.store._event(db,'capability.cancelled',row['context_id'],task_id,{'id':row['id'],'node':row['node_id']})

    def claim(self, node):
        identifier(node)
        if node == 'hub':
            raise ValueError('Built-in node cannot be claimed')
        self.tick()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM nodes WHERE id=?', (node,)).fetchone() is None:
                raise ValueError('Node is not registered')
            now = self.clock(); db.execute('UPDATE nodes SET seen_at=? WHERE id=?', (now, node))
            row = db.execute("SELECT * FROM capability_calls WHERE node_id=? AND status='queued' ORDER BY created_at LIMIT 1", (node,)).fetchone()
            if row is None:
                return None
            lease = str(uuid.uuid4())
            db.execute("UPDATE capability_calls SET status='running',lease=?,deadline=?,updated_at=? WHERE id=?", (lease, now+60, now, row['id']))
            self.store._event(db, 'capability.running', row['context_id'], row['task_id'], {'id': row['id'], 'node': node})
            return self._call(db.execute('SELECT * FROM capability_calls WHERE id=?', (row['id'],)).fetchone())

    def complete(self, node, call_id, lease, result, error=''):
        identifier(node); uid(call_id); uid(lease); text(error, 1000, empty=True)
        json.dumps(result, allow_nan=False)
        if len(encoded(result).encode()) > 12000:
            raise ValueError('Executor result exceeds budget')
        self.tick()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM capability_calls WHERE id=?', (call_id,)).fetchone()
            if row is None or row['node_id'] != node or row['lease'] != lease:
                raise Conflict('Executor claim does not match')
            status = 'failed' if error else 'succeeded'
            if row['status'] in TERMINAL:
                if row['status'] == status and row['error'] == error and json.loads(row['result']) == result:
                    return self._call(row)
                raise Conflict('Call outcome is already terminal')
            db.execute('UPDATE capability_calls SET status=?,result=?,error=?,updated_at=? WHERE id=?',
                       (status, encoded(result), error, self.clock(), call_id))
            self.store._event(db, 'capability.'+status, row['context_id'], row['task_id'], {'id': call_id, 'node': node})
        return self.result(call_id)

    def _local(self, capability, arguments, context, call_id):
        if capability == 'timer.list':
            return self.timers(context)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if capability == 'timer.create':
                timer_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'hub-timer:'+call_id))
                duration = arguments['duration_seconds']; label = arguments.get('label') or 'Таймер'
                now = self.clock()
                db.execute("INSERT INTO timers VALUES(?,?,?,?,?,?,?,?)", (timer_id, context, call_id, label, duration, now+duration, 'running', now))
                self.store._event(db, 'timer.created', context, None, {'id': timer_id, 'label': label, 'duration_seconds': duration})
            else:
                timer_id = uid(arguments['timer_id'])
                if db.execute('SELECT 1 FROM timers WHERE id=? AND context_id=?', (timer_id, context)).fetchone() is None:
                    raise ValueError('Timer is not in this topic')
                changed = db.execute("UPDATE timers SET status='cancelled' WHERE id=? AND status='running'", (timer_id,)).rowcount
                if changed:
                    self.store._event(db, 'timer.cancelled', context, None, {'id': timer_id})
            return self._timer(db.execute('SELECT * FROM timers WHERE id=?', (timer_id,)).fetchone())

    def _timer(self, row):
        return {**dict(row), 'remaining_seconds': max(0, math.ceil(row['due_at'] - self.clock())) if row['status'] == 'running' else 0}

    def timers(self, context):
        self.tick()
        with self.store.connect() as db:
            return [self._timer(r) for r in db.execute('SELECT * FROM timers WHERE context_id=? ORDER BY created_at DESC LIMIT 30', (context,))]

    def tick(self):
        now = self.clock()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT * FROM capability_calls WHERE status IN ('queued','running') AND deadline<=?", (now,)).fetchall():
                status = 'unknown' if row['status'] == 'running' else 'failed'
                error = 'Executor acknowledgement timed out; outcome unknown' if status == 'unknown' else 'Node did not claim the command before its deadline'
                db.execute('UPDATE capability_calls SET status=?,error=?,updated_at=? WHERE id=?', (status, error, now, row['id']))
                self.store._event(db, 'capability.'+status, row['context_id'], row['task_id'], {'id': row['id'], 'node': row['node_id']})
            for timer in db.execute("SELECT * FROM timers WHERE status='running' AND due_at<=?", (now,)).fetchall():
                db.execute("UPDATE timers SET status='fired' WHERE id=?", (timer['id'],))
                body = 'Таймер «'+timer['label']+'» завершён.'
                notice = str(uuid.uuid5(uuid.NAMESPACE_URL, 'timer-notification:'+timer['id']))
                db.execute('INSERT INTO notifications VALUES(?,?,?,?,?)', (notice, timer['context_id'], 'hub-timer', body, int(now)))
                self.store._event(db, 'timer.fired', timer['context_id'], None, {'id': timer['id'], 'body': body})
