"""Owner API keys and bounded, revocable browser credentials.

Random API secrets are shown once to an authenticated owner. Only SHA-256
digests are persisted. Browser passwords use salted scrypt hashes.
"""
from collections import OrderedDict
import hashlib
import secrets
import threading
import time
import uuid
import re
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
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS api_keys(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL, revoked_at REAL)''')
            if 'scope' not in {r['name'] for r in db.execute('PRAGMA table_info(api_keys)')}:
                db.execute("ALTER TABLE api_keys ADD COLUMN scope TEXT NOT NULL DEFAULT 'owner'")

    def issue(self, name, scope='owner'):
        if scope not in {'owner','app','webhook','mcp','deploy'}:raise ValueError('Invalid API key scope')
        name = text(name, 128).strip()
        if not name: raise ValueError('Укажите название приложения')
        secret = 'hub_' + secrets.token_urlsafe(48)
        row = {'id':str(uuid.uuid4()), 'name':name, 'created_at':time.time(), 'revoked_at':None,'scope':scope}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM api_keys WHERE revoked_at IS NULL').fetchone()[0] >= MAX_KEYS:
                raise ValueError('Too many active API keys; revoke an unused key first')
            db.execute('INSERT INTO api_keys(id,name,digest,created_at,scope) VALUES(?,?,?,?,?)',
                       (row['id'], name, digest(secret), row['created_at'],scope))
        return row, secret

    def list(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('''SELECT id,name,created_at,revoked_at,scope
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
        if principal and principal.startswith('account:'):
            return WebAccount(self.store).active(principal)
        if principal == OWNER:
            return bool(self.owner_digest)
        with self.store.connect() as db:
            return bool(db.execute('SELECT 1 FROM api_keys WHERE id=? AND revoked_at IS NULL', (principal,)).fetchone())

    def scope(self, principal):
        if principal and principal.startswith('account:'):
            return 'owner' if self.active(principal) else None
        if principal == OWNER:return 'owner'
        with self.store.connect() as db:
            row=db.execute('SELECT scope FROM api_keys WHERE id=? AND revoked_at IS NULL',(principal,)).fetchone()
            return row['scope'] if row else None


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

    def revoke(self, value):
        with self.lock:
            self.sessions.pop(value, None)

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


class Forbidden(PermissionError):
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
        self.password_peers = OrderedDict()
        self.password_window, self.password_total = -1, 0

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

    def check_password_attempt(self, peer):
        with self.lock:
            if self.password_window == int(time.monotonic() // 60) and (self.password_peers.get(peer, 0) >= 20 or self.password_total >= 200):
                raise RateLimited('Слишком много попыток входа. Повторите через минуту.')

    def reject_password(self, peer):
        # Expired cookies and unauthenticated background reads must not consume
        # the password budget or prevent a correct login after a restart.
        window = int(time.monotonic() // 60)
        with self.lock:
            if self.password_window != window:
                self.password_window, self.password_total = window, 0
                self.password_peers.clear()
            self.password_total += 1
            count = self.password_peers.pop(peer, 0) + 1
            self.password_peers[peer] = count
            if len(self.password_peers) > 1024: self.password_peers.popitem(last=False)
            if count > 20 or self.password_total > 200:
                raise RateLimited('Слишком много попыток входа. Повторите через минуту.')
        raise PermissionError('Неверный логин или пароль')


class WebAccount:
    """Single owner account, independently revocable browser sessions.

    API keys never become account sessions. Updating the password increments
    the session generation, invalidating all older browser credentials.
    """
    kdf_slots = threading.BoundedSemaphore(4)

    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS web_account(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL,
                username TEXT NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                generation INTEGER NOT NULL, updated_at REAL NOT NULL)''')

    @staticmethod
    def username(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{3,64}', value):
            raise ValueError('Логин: от 3 до 64 латинских букв, цифр или знаков . _ @ -')
        return value.lower()

    @classmethod
    def hash(cls, password, salt):
        with cls.kdf_slots:
            return hashlib.scrypt(password.encode('utf-8'), salt=bytes.fromhex(salt),
                n=32768, r=8, p=1, dklen=32, maxmem=64*1024*1024).hex()

    def status(self):
        with self.store.connect() as db:
            row = db.execute('SELECT username FROM web_account WHERE singleton=1').fetchone()
        return {'configured': bool(row), 'username': row['username'] if row else None}

    def active(self, principal):
        with self.store.connect() as db:
            row = db.execute('SELECT id,generation FROM web_account WHERE singleton=1').fetchone()
        return bool(row and principal == f"account:{row['id']}:{row['generation']}")

    def verify(self, username, password):
        if not isinstance(password, str) or not 1 <= len(password.encode('utf-8')) <= 1024:
            return None
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM web_account WHERE singleton=1').fetchone()
        salt = row['salt'] if row else '00'*16
        hashed = self.hash(password, salt)  # Equal KDF cost for unknown usernames.
        if row and isinstance(username, str) and username.lower() == row['username'] and secrets.compare_digest(hashed, row['password_hash']):
            return f"account:{row['id']}:{row['generation']}"
        return None

    def save(self, username, password, principal=None, current_password=None):
        username = self.username(username)
        if not isinstance(password, str) or not 12 <= len(password) or len(password.encode('utf-8')) > 1024:
            raise ValueError('Пароль: не менее 12 символов, не более 1024 байт')
        before = self.status()
        if before['configured'] and (not self.active(principal) or self.verify(before['username'], current_password) != principal):
            raise Forbidden('Введите текущий пароль')
        salt = secrets.token_hex(16); hashed = self.hash(password, salt)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT id,generation FROM web_account WHERE singleton=1').fetchone()
            if old and principal != f"account:{old['id']}:{old['generation']}":
                raise Forbidden('Учётная запись уже настроена. Войдите заново.')
            identifier = old['id'] if old else str(uuid.uuid4())
            generation = old['generation']+1 if old else 1
            db.execute('INSERT OR REPLACE INTO web_account VALUES(1,?,?,?,?,?,?)',
                (identifier, username, salt, hashed, generation, time.time()))
        return f'account:{identifier}:{generation}'
