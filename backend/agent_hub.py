"""Local foundation for shared contexts. No inference, device commands or network listener.

The CLI and future authenticated HTTP/MCP adapters use the same store. Native agent
sessions are references, not replacements for context IDs. Python 3.11+, POSIX.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import uuid
from knowledge_search import (keywords, query_terms, match_expression, excerpt,
                              MAX_FACTS, MEMORY_BYTES)


class Conflict(ValueError):
    pass


def uid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('expected canonical UUID')
    return value


def text(value, maximum, empty=False):
    if not isinstance(value, str) or '\0' in value or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError('invalid text')
    return value


def number(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('invalid number')
    return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class HubStore:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('state must be an owned private directory (0700), without symlink')
        self.path = self.directory / 'agent-hub.db'
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('unsafe state database')
        else:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS contexts(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS focus(client TEXT PRIMARY KEY, context_id TEXT NOT NULL REFERENCES contexts(id), revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    context_id TEXT NOT NULL REFERENCES contexts(id), role TEXT NOT NULL, source TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS context_entries ON entries(context_id,seq);
                CREATE TABLE IF NOT EXISTS memory(scope TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL,
                    source TEXT NOT NULL, revision INTEGER NOT NULL, deleted INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(scope,key));
                CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY, host TEXT NOT NULL, adapter TEXT NOT NULL,
                    native_id TEXT NOT NULL, title TEXT NOT NULL, cwd TEXT NOT NULL, observed_at INTEGER NOT NULL,
                    context_id TEXT REFERENCES contexts(id), UNIQUE(host,adapter,native_id));
                CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, context_id TEXT NOT NULL REFERENCES contexts(id),
                    source TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
            ''')
            self._initialize_knowledge_index(db)

    @staticmethod
    def _initialize_knowledge_index(db):
        # Separate metadata preserves the canonical memory schema and revisions.
        # FTS owns its content: ordinary DELETE/INSERT keeps tombstones out of postings.
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE INDEX IF NOT EXISTS memory_active ON memory(scope,key) WHERE deleted=0')
        fresh = not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fts'").fetchone()
        db.execute('''CREATE TABLE IF NOT EXISTS memory_keywords(scope TEXT NOT NULL,
                      key TEXT NOT NULL, words TEXT NOT NULL, PRIMARY KEY(scope,key))''')
        db.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            key,body,keywords,scope,tokenize='unicode61 remove_diacritics 2',prefix='3 4')''')
        # No Python functions inside triggers: every SQLite writer updates this index.
        insert = '''INSERT INTO memory_fts(rowid,key,body,keywords,scope)
                    SELECT new.rowid, replace(replace(new.key,'ё','е'),'Ё','Е'),
                    replace(replace(new.body,'ё','е'),'Ё','Е'),
                    coalesce((SELECT words FROM memory_keywords WHERE scope=new.scope AND key=new.key),''),replace(new.scope,'-','')
                    WHERE new.deleted=0;'''
        db.execute('CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memory BEGIN ' + insert + ' END')
        db.execute('''CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memory BEGIN
                      DELETE FROM memory_fts WHERE rowid=old.rowid;
                      DELETE FROM memory_keywords WHERE scope=old.scope AND key=old.key; END''')
        db.execute('CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE ON memory BEGIN '
                   'DELETE FROM memory_fts WHERE rowid=old.rowid; ' + insert + ' END')
        for operation in ('INSERT','UPDATE'):
            db.execute('CREATE TRIGGER IF NOT EXISTS memory_keywords_' + operation.lower() +
                       ' AFTER ' + operation + ''' ON memory_keywords BEGIN
                       UPDATE memory_fts SET keywords=new.words WHERE rowid=
                       (SELECT rowid FROM memory WHERE scope=new.scope AND key=new.key); END''')
        if fresh:
            # Only initial migration walks existing facts. Never run rebuild over tombstones.
            db.execute('''INSERT INTO memory_fts(rowid,key,body,keywords,scope)
                SELECT m.rowid,replace(replace(m.key,'ё','е'),'Ё','Е'),
                replace(replace(m.body,'ё','е'),'Ё','Е'),coalesce(k.words,''),replace(m.scope,'-','')
                FROM memory m LEFT JOIN memory_keywords k ON k.scope=m.scope AND k.key=m.key
                WHERE m.deleted=0''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def require_context(db, context):
        uid(context)
        row = db.execute('SELECT * FROM contexts WHERE id=?', (context,)).fetchone()
        if row is None:
            raise ValueError('unknown context')
        return dict(row)

    def create_context(self, title):
        text(title, 160)
        value = {'id': str(uuid.uuid4()), 'title': title, 'created_at': int(time.time())}
        with self.connect() as db:
            db.execute('INSERT INTO contexts VALUES(:id,:title,:created_at)', value)
        return value

    def contexts(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM contexts ORDER BY created_at,id')]

    def focus(self, client):
        text(client, 128)
        with self.connect() as db:
            row = db.execute('SELECT * FROM focus WHERE client=?', (client,)).fetchone()
        return dict(row) if row else {'client': client, 'context_id': None, 'revision': 0}

    def _switch(self, db, client, context, expected_revision):
        text(client, 128); number(expected_revision, 0, 2**53-1)
        self.require_context(db, context)
        previous = db.execute('SELECT revision FROM focus WHERE client=?', (client,)).fetchone()
        revision = previous['revision'] if previous else 0
        if revision != expected_revision:
            raise Conflict('focus changed; read it again before switching')
        revision += 1
        db.execute('INSERT INTO focus VALUES(?,?,?) ON CONFLICT(client) DO UPDATE SET context_id=excluded.context_id,revision=excluded.revision', (client, context, revision))
        return {'client': client, 'context_id': context, 'revision': revision}

    def switch(self, client, context, expected_revision):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._switch(db, client, context, expected_revision)

    def append(self, context, role, body, source, entry_id=None):
        if role not in {'user', 'assistant', 'tool', 'note'}:
            raise ValueError('invalid entry role')
        text(body, 20000); text(source, 256)
        entry_id = uid(entry_id) if entry_id else str(uuid.uuid4())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.require_context(db, context)
            previous = db.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone()
            if previous:
                if (previous['context_id'], previous['role'], previous['body'], previous['source']) != (context, role, body, source):
                    raise Conflict('entry ID already used with different content')
                return dict(previous)
            db.execute('INSERT INTO entries(id,context_id,role,source,body,created_at) VALUES(?,?,?,?,?,?)', (entry_id, context, role, source, body, int(time.time())))
            return dict(db.execute('SELECT * FROM entries WHERE id=?', (entry_id,)).fetchone())

    def put_memory(self, key, body, source, expected_revision, context=None, deleted=False, search_text=None):
        text(key, 160); text(body, 8000, empty=deleted); text(source, 256)
        number(expected_revision, 0, 2**53-1)
        if type(deleted) is not bool:
            raise ValueError('invalid deletion flag')
        if search_text is not None:
            text(search_text, 16000, empty=True)
        scope = context or 'global'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if context is not None:
                self.require_context(db, context)
            previous = db.execute('SELECT revision,body FROM memory WHERE scope=? AND key=?', (scope, key)).fetchone()
            revision = previous['revision'] if previous else 0
            if revision != expected_revision:
                raise Conflict('memory changed; read its revision before writing')
            if search_text is not None or deleted or (previous and previous['body'] != body):
                db.execute('''INSERT INTO memory_keywords VALUES(?,?,?) ON CONFLICT(scope,key)
                              DO UPDATE SET words=excluded.words''',
                           (scope, key, '' if deleted else keywords(search_text or '')))
            db.execute('INSERT INTO memory VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,key) DO UPDATE SET body=excluded.body,source=excluded.source,revision=excluded.revision,deleted=excluded.deleted,updated_at=excluded.updated_at',
                       (scope, key, '' if deleted else body, source, revision + 1, int(deleted), int(time.time())))
            return dict(db.execute('SELECT * FROM memory WHERE scope=? AND key=?', (scope, key)).fetchone())

    SEARCH_SQL = '''SELECT m.* FROM memory_fts JOIN memory m ON m.rowid=memory_fts.rowid
                   WHERE memory_fts MATCH ? AND rank MATCH 'bm25(4.0,1.0,0.5,0.0)'
                   AND m.deleted=0 AND m.scope IN (?,?) ORDER BY rank LIMIT ?'''

    def _search_memory(self, db, context, query, limit):
        terms = query_terms(query)
        match = match_expression(terms, context)
        if match is None:
            return [], terms
        rows = db.execute(self.SEARCH_SQL, (match, 'global', context or 'global', limit)).fetchall()
        return [dict(row) for row in rows], terms

    def search_memory(self, query, context=None, limit=MAX_FACTS):
        """Indexed scoped lookup. No matches returns no facts, without a listing fallback."""
        text(query, 16000, empty=True); number(limit, 1, 32)
        with self.connect() as db:
            if context is not None:
                self.require_context(db, context)
            return self._search_memory(db, context, query, limit)[0]

    def recent_memories(self, context=None, limit=12):
        """Explicit empty memory_read only: a bounded listing via the scope/key B-tree."""
        return self.memory_page(context,limit=limit)['items']

    def memory_page(self, context=None, limit=50, after=None):
        """Keyset pagination via the active scope/key B-tree, without COUNT or OFFSET."""
        number(limit, 1, 100)
        if after is not None:
            if not isinstance(after,(tuple,list)) or len(after)!=2:
                raise ValueError('invalid memory cursor')
            text(after[0],128);text(after[1],160)
        cursor = after or ('','')
        with self.connect() as db:
            if context is not None:
                self.require_context(db, context)
            rows = [dict(r) for r in db.execute('''SELECT * FROM memory
                WHERE scope IN (?,?) AND deleted=0 AND (scope,key)>(?,?)
                ORDER BY scope,key LIMIT ?''',('global',context or 'global',*cursor,limit+1))]
            items = rows[:limit]
            return {'items':items,'next':[items[-1]['scope'],items[-1]['key']] if len(rows)>limit else None}

    def memories(self, context=None, include_deleted=False):
        with self.connect() as db:
            if context is not None:
                self.require_context(db, context)
            scopes = ('global', context or 'global')
            return [dict(row) for row in db.execute('SELECT * FROM memory WHERE scope IN (?,?) AND (? OR deleted=0) ORDER BY scope,key', (*scopes, int(include_deleted)))]

    def register_chat(self, host, adapter, native_id, title='', cwd='', context=None):
        for value, limit in ((host,128), (adapter,80), (native_id,256)):
            text(value, limit)
        text(title, 160, empty=True); text(cwd, 4096, empty=True)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if context is not None:
                self.require_context(db, context)
            existing = db.execute('SELECT * FROM chats WHERE host=? AND adapter=? AND native_id=?', (host, adapter, native_id)).fetchone()
            chat_id = existing['id'] if existing else str(uuid.uuid4())
            # Discovery refreshes metadata; it cannot silently rebind a chat to another topic.
            if existing and context is not None and existing['context_id'] not in (None, context):
                raise Conflict('chat already bound to a different context')
            bound = context if context is not None else existing['context_id'] if existing else None
            db.execute('INSERT INTO chats VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(host,adapter,native_id) DO UPDATE SET title=excluded.title,cwd=excluded.cwd,observed_at=excluded.observed_at,context_id=excluded.context_id',
                       (chat_id, host, adapter, native_id, title, cwd, int(time.time()), bound))
            return dict(db.execute('SELECT * FROM chats WHERE id=?', (chat_id,)).fetchone())

    def chats(self, context=None):
        with self.connect() as db:
            if context is not None:
                self.require_context(db, context)
            rows = db.execute('SELECT * FROM chats WHERE context_id=? ORDER BY host,adapter,native_id', (context,)) if context else db.execute('SELECT * FROM chats ORDER BY host,adapter,native_id')
            return [dict(row) for row in rows]

    def notify(self, context, source, body):
        text(source, 256); text(body, 4000)
        with self.connect() as db:
            self.require_context(db, context)
            result = {'id': str(uuid.uuid4()), 'context_id': context, 'source': source, 'body': body, 'created_at': int(time.time())}
            db.execute('INSERT INTO notifications VALUES(:id,:context_id,:source,:body,:created_at)', result)
            return result

    def notifications(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM notifications ORDER BY created_at,id')]

    def follow_notification(self, client, notification, expected_revision):
        uid(notification)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT context_id FROM notifications WHERE id=?', (notification,)).fetchone()
            if row is None:
                raise ValueError('unknown notification')
            return self._switch(db, client, row['context_id'], expected_revision)

    def packet(self, context, budget=24000, query=None):
        """Export selected-context data, not instructions or a model-generated summary.

        Budget counts UTF-8 JSON bytes, not tokens. Omitted records are explicit;
        full records remain in the store. A single read transaction is a snapshot.
        """
        number(budget, 2048, 256000)
        if query is not None:
            text(query, 16000, empty=True)
        with self.connect() as db:
            db.execute('BEGIN')
            info = self.require_context(db, context)
            retrieval = None
            if query is None:
                # Explicit manual export, not an inference path.
                facts = db.execute('SELECT * FROM memory WHERE scope IN (?,?) AND deleted=0 ORDER BY scope,key', ('global',context)).fetchall()
            else:
                started = time.perf_counter()
                found, terms = self._search_memory(db, context, query, MAX_FACTS+1)
                facts = []
                for fact in found[:MAX_FACTS]:
                    fact['body'], truncated = excerpt(fact['body'], terms)
                    if truncated:
                        fact['bodyTruncated'] = True
                    facts.append(fact)
                retrieval = {'method':'sqlite-fts5','terms':terms,'limit':MAX_FACTS,
                             'hasMore':len(found)>MAX_FACTS,'selected':len(facts),
                             'elapsedMs':round((time.perf_counter()-started)*1000,3)}
            chats = db.execute('SELECT * FROM chats WHERE context_id=? ORDER BY host,adapter,native_id', (context,)).fetchall()
            count = db.execute('SELECT count(*) FROM entries WHERE context_id=?', (context,)).fetchone()[0]
            # Read a bounded tail; long histories need later paginated retrieval.
            entries = db.execute('SELECT * FROM entries WHERE context_id=? ORDER BY seq DESC LIMIT 100', (context,)).fetchall()
            value = {'version': 1, 'context': info, 'contentKind': 'context-data', 'memory': [], 'chats': [], 'entries': [],
                     'omitted': {'memory': len(facts), 'chats': len(chats), 'entries': count}}
            if retrieval is not None:
                value['retrieval'] = retrieval
                retrieval['memoryBytes'] = 0
            for field, candidates in (('memory',facts), ('chats',chats), ('entries',entries)):
                for row in candidates:
                    value[field].append(dict(row)); value['omitted'][field] -= 1
                    if (len(encoded(value).encode('utf-8')) > budget or
                        (query is not None and field=='memory' and len(encoded(value['memory']).encode('utf-8')) > MEMORY_BYTES)):
                        value[field].pop(); value['omitted'][field] += 1
            if retrieval is not None:
                retrieval['selected'] = len(value['memory'])
                retrieval['memoryBytes'] = len(encoded(value['memory']).encode('utf-8'))
                # Changes in statistics must not push an otherwise full packet over its budget.
                for field in ('entries','chats','memory'):
                    while value[field] and len(encoded(value).encode('utf-8')) > budget:
                        value[field].pop(); value['omitted'][field] += 1
                retrieval['selected'] = len(value['memory'])
                retrieval['memoryBytes'] = len(encoded(value['memory']).encode('utf-8'))
            value['entries'].sort(key=lambda entry: entry['seq'])
            return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('context-create'); p.add_argument('title')
    commands.add_parser('contexts')
    p = commands.add_parser('focus'); p.add_argument('client')
    p = commands.add_parser('use'); p.add_argument('client'); p.add_argument('context'); p.add_argument('--revision', type=int, required=True)
    p = commands.add_parser('append'); p.add_argument('context'); p.add_argument('--role', choices=['user','assistant','tool','note'], required=True); p.add_argument('--source', required=True); p.add_argument('--id'); p.add_argument('body')
    p = commands.add_parser('memory-put'); p.add_argument('key'); p.add_argument('body'); p.add_argument('--source', required=True); p.add_argument('--context'); p.add_argument('--revision', type=int, required=True)
    p = commands.add_parser('memory-delete'); p.add_argument('key'); p.add_argument('--source', required=True); p.add_argument('--context'); p.add_argument('--revision', type=int, required=True)
    p = commands.add_parser('memory'); p.add_argument('--context'); p.add_argument('--include-deleted', action='store_true')
    p = commands.add_parser('chat-register'); p.add_argument('--host', required=True); p.add_argument('--adapter', required=True); p.add_argument('--native-id', required=True); p.add_argument('--title', default=''); p.add_argument('--cwd', default=''); p.add_argument('--context')
    p = commands.add_parser('chats'); p.add_argument('--context')
    p = commands.add_parser('notify'); p.add_argument('context'); p.add_argument('body'); p.add_argument('--source', required=True)
    commands.add_parser('notifications')
    p = commands.add_parser('follow'); p.add_argument('client'); p.add_argument('notification'); p.add_argument('--revision', type=int, required=True)
    p = commands.add_parser('export'); p.add_argument('context'); p.add_argument('--budget', type=int, default=24000)
    args = parser.parse_args(argv)
    try:
        store = HubStore(args.state)
        if args.command == 'context-create': result = store.create_context(args.title)
        elif args.command == 'contexts': result = store.contexts()
        elif args.command == 'focus': result = store.focus(args.client)
        elif args.command == 'use': result = store.switch(args.client, args.context, args.revision)
        elif args.command == 'append': result = store.append(args.context, args.role, args.body, args.source, args.id)
        elif args.command == 'memory-put': result = store.put_memory(args.key, args.body, args.source, args.revision, args.context)
        elif args.command == 'memory-delete': result = store.put_memory(args.key, '', args.source, args.revision, args.context, deleted=True)
        elif args.command == 'memory': result = store.memories(args.context, args.include_deleted)
        elif args.command == 'chat-register': result = store.register_chat(args.host, args.adapter, args.native_id, args.title, args.cwd, args.context)
        elif args.command == 'chats': result = store.chats(args.context)
        elif args.command == 'notify': result = store.notify(args.context, args.source, args.body)
        elif args.command == 'notifications': result = store.notifications()
        elif args.command == 'follow': result = store.follow_notification(args.client, args.notification, args.revision)
        else: result = store.packet(args.context, args.budget)
        print(encoded(result)); return 0
    except (ValueError, sqlite3.Error, OSError) as error:
        print(encoded({'error': type(error).__name__, 'message': str(error)}), file=sys.stderr); return 2


if __name__ == '__main__':
    raise SystemExit(main())
