"""Topic-scoped, durable task review. No scheduler, model writes or RCP calls.

All mutations and queue inserts share the gateway's SQLite transaction. The
client's operation UUID is a durable receipt, independent of HTTP request IDs.
"""
from copy import deepcopy
import hashlib
import json
import math
import re
import time
from urllib.parse import quote
import uuid


class ReviewConflict(ValueError):
    """Revision or idempotency conflict: read state before changing intent."""


class ReviewUnavailable(ValueError):
    def __init__(self, snapshot):
        super().__init__('источник недоступен')
        self.observation = {k: snapshot.get(k) for k in ('source_status', 'checked_at', 'last_success_at')}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Expected canonical UUID')
    return value


def text(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValueError('Missing or oversized text')
    return value.strip()


def typed_ref(kind, value):
    if kind not in {'product', 'feature', 'integration'}:
        raise ValueError('Unknown RCP reference kind')
    return 'rcp:' + kind + '/' + quote(text(value, 256), safe='')


def overview_packet(snapshot, offset=0, limit=15):
    """Bounded, whitelisted data for Hermes; no transcripts or arbitrary fields."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError('Invalid overview page')
    def clip(value):
        if isinstance(value, str):
            return value.encode()[:1024].decode('utf-8', errors='ignore')
        return None
    fields = ('ref', 'title', 'expected_result', 'task_direction', 'last_confirmed_action',
              'action_evidence', 'next_step', 'next_step_kind', 'blocker', 'expected_decision',
              'status', 'status_label', 'reason', 'observed_at', 'last_progress_at', 'is_live',
              'previous_status', 'contentKind')
    tasks = []
    for row in snapshot['tasks'][offset:offset + limit]:
        # Do not truncate identity into a different valid target.
        if len(row['ref'].encode()) > 1024 or any(
                isinstance(row['source'].get(k), str) and len(row['source'][k].encode()) > 1024
                for k in ('project_id', 'session_id', 'link')):
            raise ValueError('Oversized task identity')
        task = {k: clip(row.get(k)) for k in fields}
        for key in ('observed_at', 'last_progress_at'):
            value = row.get(key)
            task[key] = value if type(value) in (int, float) and math.isfinite(value) else None
        task['is_live'] = row.get('is_live') is True
        task['source'] = {k: row['source'].get(k) for k in ('kind', 'project_id', 'session_id', 'link')}
        task['title'] = task['title'] or task['source']['session_id']
        task['executor'] = {k: clip(row['executor'].get(k)) for k in ('session_id', 'harness')}
        if len(canonical(tasks + [task]).encode()) > 62000:
            break
        tasks.append(task)
    result = {k: snapshot.get(k) for k in ('source', 'source_status', 'source_status_label',
              'checked_at', 'last_success_at', 'is_live', 'error')}
    result.update(tasks=tasks, offset=offset, total=len(snapshot['tasks']),
                  next_offset=offset + len(tasks) if offset + len(tasks) < len(snapshot['tasks']) else None)
    return result


SCHEMA = '''
CREATE TABLE IF NOT EXISTS review_sessions (
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, revision INTEGER NOT NULL,
 state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_operations (
 id TEXT PRIMARY KEY, topic TEXT NOT NULL, session TEXT NOT NULL, fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_instructions (
 id TEXT PRIMARY KEY, session TEXT NOT NULL, context TEXT NOT NULL,
 decision TEXT NOT NULL UNIQUE, reconciled_at REAL, reconciliation TEXT);
CREATE INDEX IF NOT EXISTS review_topic ON review_sessions(topic, updated);
'''


class Reviews:
    def __init__(self, db, tasks, clock=time.time):
        self.db, self.tasks, self.clock = db, tasks, clock
        with db() as connection:
            connection.executescript(SCHEMA)

    def _load(self, db, topic, session):
        row = db.execute('SELECT * FROM review_sessions WHERE id=? AND topic=?',
                         (identifier(session), topic)).fetchone()
        if not row:
            raise ValueError('Review not found in this topic')
        return row, json.loads(row['state'])

    def _view(self, db, row, state):
        result = deepcopy(state)
        result.update(id=row['id'], revision=row['revision'], created_at=row['created'],
                      updated_at=row['updated'], contentKind='untrusted-review-data')
        result['current'] = result['agenda'][result['cursor']] if result['cursor'] < len(result['agenda']) else None
        instructions = [dict(r) for r in db.execute('''SELECT i.id,i.context,i.decision,
            i.reconciled_at,i.reconciliation,c.engine,c.task,c.status,c.error
            FROM review_instructions i JOIN commands c ON c.id=i.id WHERE i.session=?
            ORDER BY i.rowid''', (row['id'],))]
        for item in instructions:
            # Native CLI success confirms only the transport's documented boundary.
            item['delivery_confirmed'] = False
            item['task_completed'] = False
            item['meaning'] = {'queued': 'accepted_into_queue', 'dispatched': 'dispatch_started',
                               'completed': 'native_command_completed', 'unknown': 'delivery_unknown'}[item['status']]
        result['instructions'] = instructions
        result['summary'] = {'decisions': [dict(d, context_id=a['context_id'], task_ref=a['task']['ref'])
                                           for a in result['agenda'] for d in a['decisions']],
                             'handed_to': [{'id': i['id'], 'executor': i['task'], 'status': i['status'],
                                            'meaning': i['meaning']} for i in instructions],
                             'waiting_for': [{'context_id': a['context_id'], 'text': d['waiting_for']}
                                             for a in result['agenda'] for d in a['decisions'] if d['waiting_for']]}
        return result

    def read(self, topic, session=None):
        with self.db() as db:
            if session is None:
                row = db.execute('SELECT id FROM review_sessions WHERE topic=? ORDER BY updated DESC,rowid DESC LIMIT 1', (topic,)).fetchone()
                if not row:
                    return None
                session = row['id']
            row, state = self._load(db, topic, session)
            return self._view(db, row, state)

    def _retry(self, db, topic, operation, fingerprint):
        receipt = db.execute('SELECT * FROM review_operations WHERE id=?', (operation,)).fetchone()
        if receipt:
            if receipt['topic'] != topic or receipt['fingerprint'] != fingerprint:
                raise ReviewConflict('Operation ID already has different intent')
            row, state = self._load(db, topic, receipt['session'])
            return self._view(db, row, state)
        return None

    def start(self, topic, operation, refs=None, urgent_refs=None):
        identifier(operation)
        text(topic, 256)  # Validate without changing the opaque topic identity.
        if refs is not None and (not isinstance(refs, list) or not 1 <= len(refs) <= 30 or
                                 any(not isinstance(r, str) for r in refs) or len(set(refs)) != len(refs)):
            raise ValueError('Expected 1..30 distinct task refs')
        urgent_refs = urgent_refs or []
        if not isinstance(urgent_refs, list) or any(not isinstance(r, str) for r in urgent_refs):
            raise ValueError('Expected urgent task references')
        fingerprint = canonical({'action': 'start', 'refs': refs, 'urgent_refs': urgent_refs})
        # Retry is checked before a network read: a lost response needs no live AO.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self._retry(db, topic, operation, fingerprint)
            if old is not None:
                return old
            active = db.execute('SELECT * FROM review_sessions WHERE topic=? ORDER BY updated DESC,rowid DESC LIMIT 1', (topic,)).fetchone()
            if active and json.loads(active['state'])['status'] == 'active' and refs is None and not urgent_refs:
                db.execute('INSERT INTO review_operations VALUES(?,?,?,?)', (operation, topic, active['id'], fingerprint))
                return self._view(db, active, json.loads(active['state']))
        snapshot = self.tasks.overview()
        if snapshot['source_status'] != 'available':
            raise ReviewUnavailable(snapshot)
        available = {r['ref']: r for r in snapshot['tasks']}
        if refs is None:
            if len(available) > 30:
                raise ValueError('Choose at most 30 task refs from the paged overview')
            refs = list(available)
        if any(r not in available for r in refs):
            raise ValueError('Unknown AO task reference')
        if set(urgent_refs) - set(refs):
            raise ValueError('Urgent refs must be part of the agenda')
        # No priority is invented from task text. Structured urgency, then blockers,
        # then decisions, then the remaining tasks. Stable order within each group.
        def category(r):
            return ('urgent' if r['ref'] in urgent_refs else 'blockers' if r.get('blocker')
                    else 'decisions' if r.get('expected_decision') else 'other')
        rank = {'urgent': 0, 'blockers': 1, 'decisions': 2, 'other': 3}
        rows = sorted((available[r] for r in refs), key=lambda r: rank[category(r)])
        bounded = [overview_packet(dict(snapshot, tasks=[row]), limit=1)['tasks'][0] for row in rows]
        for task in bounded:
            task['is_live'] = False
        agenda = [{'context_id': str(uuid.uuid4()), 'category': category(raw), 'task': task,
                   'deferred': False, 'decisions': [], 'refs': []} for raw, task in zip(rows, bounded)]
        state = {'status': 'active', 'cursor': 0, 'agenda': agenda,
                 'historical_snapshot': True,
                 'source_observation': {k: snapshot.get(k) for k in ('source_status', 'checked_at', 'last_success_at')}}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self._retry(db, topic, operation, fingerprint)
            if old is not None:
                return old
            # A new HTTP/request UUID resumes the active review, not another agenda.
            active = db.execute("SELECT * FROM review_sessions WHERE topic=? ORDER BY updated DESC,rowid DESC LIMIT 1", (topic,)).fetchone()
            if active and json.loads(active['state'])['status'] == 'active':
                if set(refs) != {a['task']['ref'] for a in json.loads(active['state'])['agenda']}:
                    raise ReviewConflict('Active review has a different agenda')
                sid = active['id']
            else:
                sid, now = str(uuid.uuid4()), self.clock()
                db.execute('INSERT INTO review_sessions VALUES(?,?,?,?,?,?)', (sid, topic, 1, canonical(state), now, now))
            db.execute('INSERT INTO review_operations VALUES(?,?,?,?)', (operation, topic, sid, fingerprint))
            row, saved = self._load(db, topic, sid)
            return self._view(db, row, saved)

    def change(self, topic, session, operation, revision, action, **data):
        result = self._change(topic, session, operation, revision, action, **data)
        if action == 'dispatch':
            for item in result['instructions']:
                if item['decision'] == data.get('decision_id') and item['status'] == 'unknown':
                    return self.reconcile(topic, session, item['id'])
        return result

    def _change(self, topic, session, operation, revision, action, **data):
        identifier(operation)
        fingerprint = canonical(dict(session=session, revision=revision, action=action, data=data))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self._retry(db, topic, operation, fingerprint)
            if old is not None:
                return old
            row, state = self._load(db, topic, session)
            if type(revision) is not int or row['revision'] != revision:
                raise ReviewConflict('Revision changed; read the review before retrying')
            if state['status'] != 'active':
                raise ReviewConflict('Review is finished')
            current = state['agenda'][state['cursor']] if state['cursor'] < len(state['agenda']) else None
            if action in {'next', 'defer', 'back', 'finish'}:
                if data:
                    raise ValueError('Unexpected navigation arguments')
                if action == 'finish':
                    state['status'] = 'finished'
                elif action == 'back':
                    state['cursor'] = max(0, state['cursor'] - 1)
                else:
                    if current and action == 'defer':
                        current['deferred'] = True
                    state['cursor'] = min(len(state['agenda']), state['cursor'] + 1)
            elif action == 'decision':
                if not current or set(data) - {'context_id', 'body', 'reason', 'waiting_for', 'refs'}:
                    raise ValueError('Invalid decision arguments')
                if data.get('context_id') != current['context_id']:
                    raise ReviewConflict('Task context changed')
                refs = data.get('refs', [])
                if not isinstance(refs, list) or len(refs) > 12 or any(
                        not isinstance(r, str) or len(r.encode()) > 1024 or
                        not re.fullmatch(r'rcp:(product|feature|integration)/[^/\s]+', r) for r in refs):
                    raise ValueError('Expected typed RCP references')
                decision = {'id': operation, 'body': text(data.get('body')), 'reason': text(data.get('reason', 'Owner decision')),
                            'waiting_for': text(data['waiting_for']) if data.get('waiting_for') else None,
                            'created_at': self.clock(), 'source': 'owner', 'refs': refs}
                if sum(len(a['decisions']) for a in state['agenda']) >= 60:
                    raise ValueError('Review decision limit reached')
                current['decisions'].append(decision)
                current['refs'] = list(dict.fromkeys(current['refs'] + refs))
            elif action == 'dispatch':
                if not current or set(data) != {'context_id', 'decision_id', 'text'} or data['context_id'] != current['context_id']:
                    raise ReviewConflict('Invalid dispatch context')
                if data['decision_id'] not in {d['id'] for d in current['decisions']}:
                    raise ValueError('Save a decision for this task first')
                prompt = text(data['text'], 16000)
                source = current['task']['source']
                if source['kind'] != 'ao' or not source['session_id'] or source['session_id'].startswith('-'):
                    raise ValueError('No supported AO executor')
                previous = db.execute('''SELECT c.* FROM review_instructions i JOIN commands c ON c.id=i.id
                    WHERE i.decision=?''', (data['decision_id'],)).fetchone()
                if previous:
                    if previous['prompt'] != prompt:
                        raise ReviewConflict('Decision already has a different instruction')
                    # Same decision always keeps its original queue UUID, including unknown.
                else:
                    uncertain = db.execute('''SELECT id FROM commands
                        WHERE engine='orchestrator' AND task=? AND status IN ('unknown','dispatched')''',
                        (source['session_id'],)).fetchone()
                    if uncertain:
                        raise ReviewConflict('Inspect uncertain instruction through reconcile; no automatic resend')
                    db.execute('INSERT INTO commands VALUES(?,?,?,?,?,?)',
                               (operation, 'orchestrator', source['session_id'], prompt, 'queued', ''))
                    db.execute('INSERT INTO review_instructions VALUES(?,?,?,?,NULL,NULL)',
                               (operation, session, current['context_id'], data['decision_id']))
            else:
                raise ValueError('Unknown review action')
            now = self.clock()
            db.execute('UPDATE review_sessions SET state=?,revision=revision+1,updated=? WHERE id=?', (canonical(state), now, session))
            db.execute('INSERT INTO review_operations VALUES(?,?,?,?)', (operation, topic, session, fingerprint))
            row, saved = self._load(db, topic, session)
            return self._view(db, row, saved)

    def reconcile(self, topic, session, instruction):
        """Read before considering uncertain delivery. Reports cannot prove delivery."""
        with self.db() as db:
            self._load(db, topic, session)
            row = db.execute('''SELECT c.* FROM commands c JOIN review_instructions i ON i.id=c.id
                WHERE i.session=? AND i.id=?''', (session, identifier(instruction))).fetchone()
            if not row:
                raise ValueError('Unknown review instruction')
        # Never execute, requeue or interpret report text as an acknowledgement.
        report = self.tasks.read(row['engine'], row['task'])
        digest = hashlib.sha256(canonical(report).encode()).hexdigest()
        with self.db() as db:
            db.execute('UPDATE review_instructions SET reconciled_at=?,reconciliation=? WHERE id=?',
                       (self.clock(), 'native_read_sha256:' + digest, instruction))
        return self.read(topic, session)
