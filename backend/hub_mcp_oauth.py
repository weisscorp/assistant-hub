"""Personal Hub OAuth 2.1 authorization-code/PKCE boundary for a ChatGPT plugin.

No OpenAI API key or model API is involved. Only a Hub owner can approve a grant.
Opaque access/refresh tokens and codes are stored as digests and bound to /mcp.
"""
import base64
import hashlib
import html
import json
import re
import secrets
import time
import urllib.parse
from agent_hub import encoded, text
from hub_auth import digest

SCOPE = 'hub:requests'
REDIRECT = 'https://chatgpt.com/connector_platform_oauth_redirect'
PKCE = re.compile(r'^[A-Za-z0-9_-]{43}$')
VERIFIER = re.compile(r'^[A-Za-z0-9._~-]{43,128}$')


class OAuthError(ValueError):
    pass


class McpOAuth:
    def __init__(self, app):
        self.app, self.store = app, app.store
        self.issuer = app.proxy.origin
        self.resource = self.issuer + '/mcp' if self.issuer else None
        self.redirects = set((app.config.get('mcp_events') or {}).get('oauth_redirect_uris', [REDIRECT]))
        if not self.redirects or len(self.redirects) > 8 or any(not isinstance(x, str) or not x.startswith('https://chatgpt.com/') or urllib.parse.urlsplit(x).fragment for x in self.redirects):
            raise ValueError('Configure exact ChatGPT OAuth redirect URIs')
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS mcp_oauth_clients(id TEXT PRIMARY KEY,name TEXT NOT NULL,redirects TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mcp_oauth_pending(digest TEXT PRIMARY KEY,params TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS mcp_oauth_codes(digest TEXT PRIMARY KEY,principal TEXT NOT NULL,
                    client TEXT NOT NULL,redirect TEXT NOT NULL,challenge TEXT NOT NULL,resource TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS mcp_oauth_tokens(digest TEXT PRIMARY KEY,principal TEXT NOT NULL,
                    client TEXT NOT NULL,resource TEXT NOT NULL,kind TEXT NOT NULL,expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS mcp_oauth_expiration ON mcp_oauth_tokens(expires);
            ''')

    def enabled(self):
        return self.app.mcp.enabled and bool(self.issuer)

    def metadata(self, resource=False):
        if not self.enabled():
            raise OAuthError('Configure public_url for OAuth discovery')
        if resource:
            return {'resource': self.resource, 'authorization_servers': [self.issuer], 'scopes_supported': [SCOPE], 'bearer_methods_supported': ['header']}
        return {'issuer': self.issuer, 'authorization_endpoint': self.issuer+'/oauth/authorize',
                'token_endpoint': self.issuer+'/oauth/token', 'registration_endpoint': self.issuer+'/oauth/register',
                'response_types_supported': ['code'], 'grant_types_supported': ['authorization_code', 'refresh_token'],
                'token_endpoint_auth_methods_supported': ['none'], 'code_challenge_methods_supported': ['S256'],
                'scopes_supported': [SCOPE], 'authorization_response_iss_parameter_supported': True,
                'client_id_metadata_document_supported': False}

    def register(self, body):
        self.metadata()
        redirects = body.get('redirect_uris')
        if not isinstance(redirects, list) or not 1 <= len(redirects) <= 4 or any(x not in self.redirects for x in redirects):
            raise OAuthError('invalid_redirect_uri')
        if body.get('token_endpoint_auth_method', 'none') != 'none':
            raise OAuthError('Public PKCE clients only')
        grants = body.get('grant_types', ['authorization_code', 'refresh_token'])
        if not isinstance(grants, list) or 'authorization_code' not in grants or not set(grants) <= {'authorization_code','refresh_token'} or body.get('response_types', ['code']) != ['code']:
            raise OAuthError('Unsupported OAuth flow')
        client_id = 'hubclient_' + secrets.token_urlsafe(24)
        name = text(body.get('client_name', 'ChatGPT dot'), 100)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM mcp_oauth_clients').fetchone()[0] >= 64:
                raise OAuthError('Client registration limit reached')
            db.execute('INSERT INTO mcp_oauth_clients VALUES(?,?,?)', (client_id, name, encoded(redirects)))
        return {'client_id': client_id, 'client_name': name, 'redirect_uris': redirects,
                'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code']}

    def validate_authorization(self, params):
        self.metadata()
        with self.store.connect() as db:
            client = db.execute('SELECT * FROM mcp_oauth_clients WHERE id=?', (params.get('client_id'),)).fetchone()
        if not client or params.get('redirect_uri') not in json.loads(client['redirects']):
            raise OAuthError('Invalid client or redirect')
        if (params.get('response_type') != 'code' or params.get('code_challenge_method') != 'S256'
                or not PKCE.fullmatch(params.get('code_challenge', ''))
                or params.get('resource') != self.resource or params.get('scope', SCOPE) != SCOPE):
            raise OAuthError('Invalid resource, scope or PKCE challenge')
        text(params.get('state'), 2048)
        return client

    def consent(self, params):
        client = self.validate_authorization(params)
        nonce = secrets.token_urlsafe(32)
        now = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM mcp_oauth_pending WHERE expires<=?', (now,))
            if db.execute('SELECT count(*) FROM mcp_oauth_pending').fetchone()[0] >= 128:
                raise OAuthError('Too many pending authorizations')
            db.execute('INSERT INTO mcp_oauth_pending VALUES(?,?,?)', (digest(nonce), encoded(params), now+300))
        page = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Подключить dot к Assistant Hub</title><style>body{background:#0a131f;color:#e6eff9;font:18px system-ui;margin:0;padding:32px}main{max-width:640px;margin:8vh auto}input,button{box-sizing:border-box;width:100%%;padding:16px;border-radius:12px;margin:12px 0;font:inherit}button{background:#88ddcf;color:#08212a;border:0;cursor:pointer}small{overflow-wrap:anywhere;color:#aebed0}</style><main>
<h1>Подключить dot к Hub</h1><p>Клиент: %s</p><p>Разрешение: читать контекст запросов и общие знания, сохранять знания, вызывать доступные действия устройств в рамках активного запроса и возвращать ответы.</p>
<small>Ответ авторизации получит: %s</small><form method="post" action="/oauth/authorize"><input type="hidden" name="nonce" value="%s"><label for="token">Ключ владельца Hub</label><input id="token" name="token" type="password" autocomplete="current-password" required><button type="submit">Подключить</button></form><p>Доступ можно отозвать в списке API-ключей Hub.</p></main></html>''' % (html.escape(client['name']), html.escape(params['redirect_uri']), nonce)
        return page.encode(), nonce

    def approve(self, nonce, cookie_nonce, owner_secret):
        self.metadata()
        if not isinstance(nonce, str) or not isinstance(cookie_nonce, str) or not secrets.compare_digest(nonce, cookie_nonce):
            raise OAuthError('Invalid consent nonce')
        owner = self.app.auth.resolve(owner_secret)
        if self.app.auth.scope(owner) != 'owner':
            raise PermissionError('A valid Hub owner key is required')
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM mcp_oauth_pending WHERE digest=? AND expires>?', (digest(nonce), time.time())).fetchone()
        if not row:
            raise OAuthError('Consent expired or already used')
        params = json.loads(row['params'])
        client = self.validate_authorization(params)
        grant, _ = self.app.auth.issue('dot: '+client['name'], 'mcp')
        code = secrets.token_urlsafe(32)
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('DELETE FROM mcp_oauth_pending WHERE digest=? AND expires>?', (digest(nonce), time.time())).rowcount != 1:
                    raise OAuthError('Consent expired or already used')
                db.execute('INSERT INTO mcp_oauth_codes VALUES(?,?,?,?,?,?,?)',
                           (digest(code), grant['id'], params['client_id'], params['redirect_uri'], params['code_challenge'], self.resource, time.time()+60))
        except Exception:
            self.app.auth.revoke(grant['id'])
            raise
        separator = '&' if '?' in params['redirect_uri'] else '?'
        return params['redirect_uri'] + separator + urllib.parse.urlencode({'code':code, 'state':params['state'], 'iss':self.issuer})

    def exchange(self, form):
        self.metadata()
        kind = form.get('grant_type')
        if form.get('resource') != self.resource:
            raise OAuthError('invalid_target')
        now = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if kind == 'authorization_code':
                value = digest(form.get('code'))
                row = db.execute('SELECT * FROM mcp_oauth_codes WHERE digest=? AND expires>?', (value, now)).fetchone()
                verifier = form.get('code_verifier', '')
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
                if (not row or not VERIFIER.fullmatch(verifier) or not secrets.compare_digest(row['challenge'], challenge)
                        or form.get('redirect_uri') != row['redirect']):
                    raise OAuthError('invalid_grant')
            elif kind == 'refresh_token':
                value = digest(form.get('refresh_token'))
                row = db.execute("SELECT * FROM mcp_oauth_tokens WHERE digest=? AND expires>? AND kind='refresh'", (value, now)).fetchone()
                if not row:
                    raise OAuthError('invalid_grant')
            else:
                raise OAuthError('unsupported_grant_type')
            if row['client'] != form.get('client_id') or row['resource'] != self.resource:
                raise OAuthError('invalid_grant')
            # Avoid a second write connection while holding this transaction.
            if not db.execute("SELECT 1 FROM api_keys WHERE id=? AND scope='mcp' AND revoked_at IS NULL", (row['principal'],)).fetchone():
                raise OAuthError('invalid_grant')
            if kind == 'authorization_code':
                db.execute('DELETE FROM mcp_oauth_codes WHERE digest=?', (value,))
            else:
                db.execute('DELETE FROM mcp_oauth_tokens WHERE digest=?', (value,))
            access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
            db.execute('INSERT INTO mcp_oauth_tokens VALUES(?,?,?,?,?,?)', (digest(access), row['principal'], row['client'], self.resource, 'access', now+3600))
            db.execute('INSERT INTO mcp_oauth_tokens VALUES(?,?,?,?,?,?)', (digest(refresh), row['principal'], row['client'], self.resource, 'refresh', now+30*86400))
            db.execute('DELETE FROM mcp_oauth_tokens WHERE expires<=?', (now,))
            db.execute('DELETE FROM mcp_oauth_codes WHERE expires<=?', (now,))
        return {'access_token':access, 'refresh_token':refresh, 'token_type':'Bearer', 'expires_in':3600, 'scope':SCOPE}

    def resolve(self, secret):
        if not self.enabled() or digest(secret) is None:
            return None
        with self.store.connect() as db:
            row = db.execute("SELECT principal FROM mcp_oauth_tokens WHERE digest=? AND resource=? AND kind='access' AND expires>?", (digest(secret), self.resource, time.time())).fetchone()
        return row['principal'] if row and self.app.auth.scope(row['principal']) == 'mcp' else None
