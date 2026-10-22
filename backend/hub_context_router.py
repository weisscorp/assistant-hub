"""Mobile context gateway. Hub stores route pointers and request receipts only.

The configured owner process receives a single user turn and owns its history,
RCP retrieval and agent session. No mixed mobile history or Hub memory is sent.
"""
import hashlib
import json
import re
import subprocess
import time
import uuid
from agent_hub import Conflict, encoded, text


class ContextRouter:
    def __init__(self, store, config):
        self.store = store
        self.routes = {}
        for item in config.get('routes', []):
            route = dict(item)
            for key in ('id', 'label', 'owner'):
                text(route.get(key), 128)
            if route['id'] in self.routes:
                raise ValueError('Duplicate context route')
            if not isinstance(route.get('aliases'), list) or not route['aliases']:
                raise ValueError('Route needs aliases')
            for alias in route['aliases']: text(alias, 80)
            argv = route.get('argv')
            if not isinstance(argv, list) or not argv or len(argv) > 32:
                raise ValueError('Route needs a fixed owner argv')
            for arg in argv: text(arg, 2000)
            refs = route.get('context_refs', {})
            if not isinstance(refs, dict) or set(refs) - {'product_id', 'feature_id', 'thread_id', 'channel_id', 'chat_id'}:
                raise ValueError('Invalid context references')
            for value in refs.values(): text(value, 256)
            self.routes[route['id']] = route
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS context_route_focus(
                    client TEXT NOT NULL,session TEXT NOT NULL,route TEXT NOT NULL,
                    updated_at REAL NOT NULL,PRIMARY KEY(client,session));
                CREATE TABLE IF NOT EXISTS context_route_requests(
                    id TEXT PRIMARY KEY,client TEXT NOT NULL,session TEXT NOT NULL,
                    route TEXT NOT NULL,digest TEXT NOT NULL,status TEXT NOT NULL,
                    created_at REAL NOT NULL);
            ''')
            # A restarted router cannot know whether the external owner executed a turn.
            db.execute("UPDATE context_route_requests SET status='unknown' WHERE status='running'")

    @property
    def enabled(self): return bool(self.routes)

    def catalog(self):
        return [{k: r[k] for k in ('id', 'label', 'owner', 'aliases')} | {'context_refs': r.get('context_refs', {})}
                for r in self.routes.values()]

    def select(self, prompt, client, session):
        value = prompt.casefold().replace('ё', 'е')
        matches = [r for r in self.routes.values() if any(re.search(r'(?<!\w)'+re.escape(a.casefold().replace('ё','е'))+r'(?!\w)', value) for a in r['aliases'])]
        if len(matches) == 1: return matches[0]
        if len(matches) > 1: return None
        # Only explicit continuations inherit focus. A new unlabelled question asks.
        if value.strip(' .!?') in {'продолжай', 'подробнее', 'что дальше', 'да', 'нет'}:
            with self.store.connect() as db:
                row = db.execute('SELECT route FROM context_route_focus WHERE client=? AND session=? AND updated_at>?',
                                 (client, session, time.time()-1800)).fetchone()
                return self.routes.get(row['route']) if row else None
        return None

    def turn(self, body, client):
        route = self.select(body['text'], client, body['session'])
        if route is None:
            return 'К какому контексту относится вопрос: '+', '.join(r['label'] for r in self.routes.values())+'? Назовите его вместе с вопросом.'
        payload = {'version': 1, 'id': body['id'], 'session': str(uuid.uuid5(uuid.NAMESPACE_URL, client+':'+body['session']+':'+route['id'])),
                   'route': route['id'], 'owner': route['owner'], 'context_refs': route.get('context_refs', {}),
                   'text': body['text'], 'history': [], 'capabilities': body['capabilities']}
        digest = hashlib.sha256(encoded(payload).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM context_route_requests WHERE id=?', (body['id'],)).fetchone()
            if old:
                if old['digest'] != digest or old['client'] != client or old['session'] != body['session']:
                    raise Conflict('Request ID conflict')
                return 'Этот запрос уже передан владельцу контекста. Повтор автоматически не отправлен; запросите статус новым сообщением.'
            db.execute("INSERT INTO context_route_requests VALUES(?,?,?,?,?,'running',?)",
                       (body['id'], client, body['session'], route['id'], digest, time.time()))
        try:
            # Trusted configuration only. Message/refs go through stdin, never a shell.
            result = subprocess.run(route['argv'], input=encoded(payload), text=True, encoding='utf-8',
                                    capture_output=True, timeout=22, check=True)
            if len(result.stdout.encode()) > 16000: raise ValueError('Oversized owner reply')
            value = json.loads(result.stdout)
            if not isinstance(value, dict) or set(value) != {'id', 'reply'} or value['id'] != body['id']:
                raise ValueError('Invalid owner reply')
            reply = text(value['reply'], 1500)
        except (OSError, ValueError, subprocess.SubprocessError):
            with self.store.connect() as db:
                db.execute("UPDATE context_route_requests SET status='unknown' WHERE id=?", (body['id'],))
            return 'Владелец контекста «'+route['label']+'» не подтвердил ответ. Результат неизвестен; автоматически повторять запрос не буду.'
        with self.store.connect() as db:
            db.execute("UPDATE context_route_requests SET status='completed' WHERE id=?", (body['id'],))
            db.execute('INSERT INTO context_route_focus VALUES(?,?,?,?) ON CONFLICT(client,session) DO UPDATE SET route=excluded.route,updated_at=excluded.updated_at',
                       (client, body['session'], route['id'], time.time()))
        return reply
