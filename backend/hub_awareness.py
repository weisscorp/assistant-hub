"""Prototype event inbox. External text is data; decisions never execute tools.

Correlation is explicit and project-scoped. Classification uses visible rules,
not an LLM. Device publication uses the existing durable phone inbox.
"""
import hashlib
import json
import time
import uuid
from agent_hub import Conflict, encoded, text, uid


SOURCES = {'slack', 'telegram', 'codex'}
STATES = {'open', 'in_progress', 'snoozed', 'closed'}


def normalize(value):
    if not isinstance(value, dict):
        raise ValueError('Expected event object')
    allowed = {'source', 'external_id', 'project', 'correlation', 'title', 'body', 'url', 'needs_decision', 'priority'}
    if set(value) - allowed:
        raise ValueError('Unknown event fields')
    result = {k: text(value.get(k), n).strip() for k, n in
              [('source', 16), ('external_id', 160), ('project', 120), ('title', 160), ('body', 8000)]}
    if result['source'] not in SOURCES:
        raise ValueError('Choose slack, telegram or codex')
    result['correlation'] = text(value.get('correlation', ''), 160, empty=True).strip()
    result['url'] = text(value.get('url', ''), 2000, empty=True)
    if result['url']:
        from urllib.parse import urlsplit
        parsed = urlsplit(result['url'])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in result['url']):
            raise ValueError('Source link must be HTTPS without credentials')
    result['needs_decision'] = value.get('needs_decision', False)
    if type(result['needs_decision']) is not bool:
        raise ValueError('needs_decision must be boolean')
    result['priority'] = value.get('priority', 'normal')
    if result['priority'] not in {'normal', 'important'}:
        raise ValueError('Invalid priority')
    return result


class Awareness:
    def __init__(self, store, link):
        self.store, self.link = store, link
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS awareness_cases(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, correlation TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL, snoozed_until REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(project,correlation));
                CREATE TABLE IF NOT EXISTS awareness_events(
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES awareness_cases(id),
                    producer TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
                    digest TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
                    UNIQUE(producer,source,external_id));
                CREATE INDEX IF NOT EXISTS awareness_case_events ON awareness_events(case_id,created_at);
                CREATE TABLE IF NOT EXISTS awareness_decisions(
                    case_id TEXT NOT NULL REFERENCES awareness_cases(id), revision INTEGER NOT NULL,
                    action TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(case_id,revision));
                CREATE TABLE IF NOT EXISTS awareness_outbox(
                    id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES awareness_cases(id),
                    revision INTEGER NOT NULL,device TEXT NOT NULL,payload TEXT NOT NULL,
                    status TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',
                    UNIQUE(case_id,revision,device));
            ''')

    def accept(self, value, producer):
        event = normalize(value)
        producer = text(producer, 128)
        digest = hashlib.sha256(encoded(event).encode()).hexdigest()
        now = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM awareness_events WHERE producer=? AND source=? AND external_id=?',
                             (producer, event['source'], event['external_id'])).fetchone()
            if old:
                if old['digest'] != digest:
                    raise Conflict('Event ID already has different content')
                return self._case(db, old['case_id'])
            if db.execute('SELECT count(*) FROM awareness_events').fetchone()[0] >= 10000:
                raise Conflict('Prototype event limit reached')
            if db.execute('SELECT count(*) FROM awareness_events WHERE producer=? AND created_at>?', (producer, now-60)).fetchone()[0] >= 120:
                raise Conflict('Too many events; retry later')
            # Namespaced keys prevent an uncorrelated event from colliding with a supplied key.
            correlation = 'explicit:' + event['correlation'] if event['correlation'] else 'event:' + str(uuid.uuid4())
            old_case = db.execute('SELECT * FROM awareness_cases WHERE project=? AND correlation=?', (event['project'], correlation)).fetchone()
            case_id = old_case['id'] if old_case else str(uuid.uuid4())
            if old_case:
                # A new event reopens a closed case; active snoozes are respected.
                db.execute("UPDATE awareness_cases SET revision=revision+1,updated_at=?,status=CASE WHEN status='closed' THEN 'open' ELSE status END WHERE id=?", (now, case_id))
            else:
                db.execute("INSERT INTO awareness_cases VALUES(?,?,?,'open',1,NULL,?,?)", (case_id, event['project'], correlation, now, now))
            db.execute('INSERT INTO awareness_events VALUES(?,?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()), case_id, producer, event['source'], event['external_id'], digest, encoded(event), now))
            return self._case(db, case_id)

    def _case(self, db, case_id):
        row = db.execute('SELECT * FROM awareness_cases WHERE id=?', (uid(case_id),)).fetchone()
        if row is None:
            raise ValueError('Unknown situation')
        result = dict(row)
        result['events'] = [dict(json.loads(e['payload']), id=e['id'], received_at=e['created_at']) for e in
                            db.execute('SELECT * FROM awareness_events WHERE case_id=? ORDER BY created_at,id', (case_id,))]
        result['needs_decision'] = any(e['needs_decision'] for e in result['events'])
        result['priority'] = 'important' if any(e['priority'] == 'important' for e in result['events']) else 'normal'
        result['title'] = result['events'][-1]['title']
        result['reason'] = 'Источник отметил вопрос к вам' if result['needs_decision'] else 'Информационное событие'
        result['classification'] = 'Правила прототипа · без нейросетевого анализа'
        result['effective_status'] = 'open' if result['status'] == 'snoozed' and result['snoozed_until'] <= time.time() else result['status']
        result['decisions'] = [dict(r) for r in db.execute('SELECT revision,action,created_at FROM awareness_decisions WHERE case_id=? ORDER BY revision', (case_id,))]
        result['deliveries'] = [dict(r) for r in db.execute('SELECT id,device,revision,status,error FROM awareness_outbox WHERE case_id=? ORDER BY revision', (case_id,))]
        return result

    def snapshot(self):
        with self.store.connect() as db:
            rows = db.execute('SELECT id FROM awareness_cases ORDER BY updated_at DESC,id LIMIT 100').fetchall()
            cases = [self._case(db, r['id']) for r in rows]
            sources = []
            for source in sorted(SOURCES):
                seen = db.execute('SELECT MAX(created_at) FROM awareness_events WHERE source=?', (source,)).fetchone()[0]
                sources.append({'source': source, 'last_received': seen, 'status': 'events_received' if seen else 'not_connected',
                                'note': 'Приём через API; постоянное подключение к сервису не настроено'})
            return {'cases': cases, 'sources': sources, 'devices': self.link.devices(),
                    'total': db.execute('SELECT count(*) FROM awareness_cases').fetchone()[0]}

    def decide(self, case_id, revision, action, seconds=3600):
        if type(revision) is not int or revision < 1 or action not in {'take', 'snooze', 'close', 'reopen'}:
            raise ValueError('Invalid decision')
        if type(seconds) is not int or not 60 <= seconds <= 604800:
            raise ValueError('Snooze must be 60 seconds to 7 days')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._case(db, case_id)
            if row['revision'] != revision:
                raise Conflict('Situation changed; refresh before deciding')
            status = {'take': 'in_progress', 'snooze': 'snoozed', 'close': 'closed', 'reopen': 'open'}[action]
            now = time.time()
            db.execute('UPDATE awareness_cases SET status=?,revision=revision+1,snoozed_until=?,updated_at=? WHERE id=?',
                       (status, now+seconds if action == 'snooze' else None, now, case_id))
            db.execute('INSERT INTO awareness_decisions VALUES(?,?,?,?)', (case_id, revision+1, action, now))
            return self._case(db, case_id)

    def publish(self, case_id, revision, device):
        device = uid(device)
        if type(revision) is not int:
            raise ValueError('Invalid revision')
        if device not in {r['device'] for r in self.link.devices()}:
            raise ValueError('Phone must already be paired')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            case = self._case(db, case_id)
            if case['revision'] != revision:
                raise Conflict('Situation changed; refresh before sending')
            delivery_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'awareness:{case_id}:{revision}:{device}'))
            payload = {'id': delivery_id, 'kind': 'message', 'source': 'assistant-hub',
                       'title': case['title'], 'body': (case['project']+' · '+case['reason']+'\n'+case['events'][-1]['body'])[:4000],
                       'priority': case['priority'], 'createdAt': int(time.time()*1000)}
            db.execute("INSERT OR IGNORE INTO awareness_outbox VALUES(?,?,?,?,?,'pending','')",
                       (delivery_id, case_id, revision, device, encoded(payload)))
            saved = dict(db.execute('SELECT * FROM awareness_outbox WHERE id=?', (delivery_id,)).fetchone())
        if saved['status'] != 'queued_on_phone':
            try:
                self.link.publish(device, json.loads(saved['payload']))
            except (ValueError, KeyError):
                with self.store.connect() as db:
                    db.execute("UPDATE awareness_outbox SET error='Inbox unavailable; retry publication' WHERE id=?", (delivery_id,))
                raise Conflict('Phone inbox unavailable; retry safely')
            with self.store.connect() as db:
                db.execute("UPDATE awareness_outbox SET status='queued_on_phone',error='' WHERE id=?", (delivery_id,))
        return {'id': delivery_id, 'status': 'queued_on_phone', 'note': 'В очереди inbox; прочтение не подтверждено'}


def demo_events():
    common = {'project': 'Работа / демо', 'correlation': 'release-42', 'needs_decision': True, 'priority': 'important'}
    return [dict(common, source='slack', external_id='demo-slack-1', title='Релиз ждёт решения',
                 body='Команда спрашивает: выпускаем сегодня или переносим до исправления?', url='https://example.test/slack/release-42'),
            dict(common, source='codex', external_id='demo-codex-1', title='Проверки нашли блокер релиза',
                 body='Тест миграции завершился ошибкой. Нужен выбор: исправить сейчас или отложить релиз.', url=''),
            dict(common, source='telegram', external_id='demo-telegram-1', title='Нужен ответ по сроку релиза',
                 body='Менеджер просит подтвердить срок. Это тот же релиз, что обсуждается в Slack.', url='https://example.test/telegram/release-42'),
            {'project': 'Свой проект / демо', 'source': 'codex', 'external_id': 'demo-codex-2', 'title': 'Исправление готово к просмотру',
             'body': 'Локальные проверки прошли. Изменения ждут твоего просмотра.', 'needs_decision': False}]
