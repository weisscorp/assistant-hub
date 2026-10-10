"""Owner API keys and bounded, revocable browser credentials.

Random API secrets are returned once to the local CLI. Only SHA-256 digests
are persisted; no endpoint creates or returns an API secret.
"""
from collections import OrderedDict
import hashlib
import secrets
import threading
import time
import uuid
from agent_hub import text, uid


OWNER = 'owner'
MAX_KEYS = 128
MAX_BROWSER_CREDENTIALS = 256


def digest(value):
    # Reject malformed/unbounded credentials without logging or reflecting them.
    if not isinstance(value, str) or not 32 <= len(value) <= 8192 or not value.isascii():
        return None
    return hashlib.sha256(value.encode('ascii')).hexdigest()


class AccessKeys:
    def __init__(self, store, owner_token=None):
        self.store = store
        self.owner_digest = digest(owner_token)
        if owner_token is not None and self.owner_digest is None:
            raise ValueError('Invalid owner bearer token')
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS api_keys(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL, revoked_at REAL)''')

    def issue(self, name):
        name = text(name, 128).strip()
        secret = 'hub_' + secrets.token_urlsafe(48)
        row = {'id':str(uuid.uuid4()), 'name':name, 'created_at':time.time(), 'revoked_at':None}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM api_keys WHERE revoked_at IS NULL').fetchone()[0] >= MAX_KEYS:
                raise ValueError('Too many active API keys; revoke an unused key first')
            db.execute('INSERT INTO api_keys VALUES(?,?,?,?,NULL)',
                       (row['id'], name, digest(secret), row['created_at']))
        return row, secret

    def list(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('''SELECT id,name,created_at,revoked_at
                FROM api_keys ORDER BY created_at DESC''')]

    def revoke(self, key_id):
        uid(key_id)
        with self.store.connect() as db:
            if not db.execute('SELECT 1 FROM api_keys WHERE id=?', (key_id,)).fetchone():
                raise ValueError('Unknown API key ID')
            db.execute('UPDATE api_keys SET revoked_at=coalesce(revoked_at,?) WHERE id=?',
                       (time.time(), key_id))

    def resolve(self, secret):
        value = digest(secret)
        if value is None:
            return None
        if self.owner_digest and secrets.compare_digest(value, self.owner_digest):
            return OWNER
        with self.store.connect() as db:
            row = db.execute('SELECT id FROM api_keys WHERE digest=? AND revoked_at IS NULL', (value,)).fetchone()
            return row['id'] if row else None

    def active(self, principal):
        if principal == OWNER:
            return bool(self.owner_digest)
        with self.store.connect() as db:
            return bool(db.execute('SELECT 1 FROM api_keys WHERE id=? AND revoked_at IS NULL', (principal,)).fetchone())


class BrowserCredentials:
    def __init__(self, keys):
        self.keys = keys
        self.lock = threading.RLock()
        self.sessions, self.tickets = OrderedDict(), OrderedDict()

    @staticmethod
    def _prune(cache):
        now = time.time()
        for key, row in list(cache.items()):
            if row['expires'] <= now:
                del cache[key]
        while len(cache) >= MAX_BROWSER_CREDENTIALS:
            cache.popitem(last=False)

    def _create(self, cache, principal, origin, ttl):
        if not self.keys.active(principal):
            raise PermissionError('Нужен ключ доступа к Hub')
        value = secrets.token_urlsafe(32)
        with self.lock:
            self._prune(cache)
            cache[value] = {'principal':principal, 'origin':origin, 'expires':time.time()+ttl}
        return value

    def session(self, principal, origin):
        return self._create(self.sessions, principal, origin, 86400)

    def ticket(self, principal, origin):
        return self._create(self.tickets, principal, origin, 60)

    def resolve(self, value, origin, ticket=False):
        with self.lock:
            cache = self.tickets if ticket else self.sessions
            row = cache.pop(value, None) if ticket else cache.get(value)
            if not row or row['expires'] <= time.time() or row['origin'] != origin:
                return None
        # Re-check the durable key on every request; revocation needs no restart.
        return row['principal'] if self.keys.active(row['principal']) else None


class RateLimited(PermissionError):
    pass


class AuthFailures:
    """Fixed one-minute failure windows, bounded by 1024 peer addresses.

    Correct credentials continue to work during a failed-authentication flood.
    Forwarded addresses must be overwritten by an explicitly trusted proxy.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.peers = OrderedDict()
        self.window, self.total = -1, 0

    def reject(self, peer):
        window = int(time.monotonic() // 60)
        with self.lock:
            if window != self.window:
                self.window, self.total = window, 0
                self.peers.clear()
            self.total += 1
            count = self.peers.pop(peer, 0) + 1
            self.peers[peer] = count
            if len(self.peers) > 1024:
                self.peers.popitem(last=False)
            limited = count > 20 or self.total > 200
        if limited:
            raise RateLimited('Слишком много запросов без верного ключа. Повторите через минуту.')
        raise PermissionError('Нужен ключ доступа к Hub')
