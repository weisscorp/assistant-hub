"""Authenticated local/VPN HTTP facade for the cross-device assistant."""
import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import os
from pathlib import Path
import socket
import stat
import threading
import time
import urllib.parse
import uuid
from agent_hub import Conflict, encoded, uid, text
from hub_runtime import Coordinator, RuntimeStore, TERMINAL
from hub_providers import OllamaProvider, NativeCodexProvider, CommandProvider, OrchestratorProvider, CloudProvider
from hub_network import bind_ip, authority, ProxyOrigin, http_origins
from hub_auth import AccessKeys, BrowserCredentials, AuthFailures, RateLimited, Forbidden, WebAccount
from hub_mcp_events import McpEvents, DotProvider
from hub_mcp_oauth import McpOAuth, OAuthError
from hub_mausbot import MausBotProvider
from hub_routing import RoutingProvider


def private_json(path):
    p = Path(path)
    i = p.lstat()
    if not stat.S_ISREG(i.st_mode) or i.st_uid != os.getuid() or i.st_mode & 0o077 or i.st_size > 64000:
        raise ValueError('Config must be an owned private regular file')
    return json.loads(p.read_bytes())


class HubApplication:
    def __init__(self, config):
        self.config = config
        self.proxy = ProxyOrigin(config)
        http_origins(config)
        self.store = RuntimeStore(config['state'])
        preserve = [name for name,value in config['providers'].items() if value.get('type')=='mcp_events'] if config.get('mcp_events') is not None else []
        self.store.recover(preserve)
        token_path=Path(config['token_file']);token_info=token_path.lstat()
        if not stat.S_ISREG(token_info.st_mode) or token_info.st_uid!=os.getuid() or token_info.st_mode&0o077 or token_info.st_size>8192:
            raise ValueError('Token must be an owned private regular file')
        self.token = token_path.read_text().strip()
        self.auth = AccessKeys(self.store, self.token)
        self.account = WebAccount(self.store)
        self.browser = BrowserCredentials(self.auth)
        self.auth_failures = AuthFailures()
        self.device = self.store.setting('desktop-device')
        if not self.device:
            self.device = str(uuid.uuid4()); self.store.set_setting('desktop-device', self.device)
        if not self.store.contexts(): self.store.create_context('Разговор')
        self.providers = {}
        for name, value in config['providers'].items():
            if value['type'] == 'routing': continue
            if value['type'] == 'ollama': self.providers[name] = OllamaProvider(value['url'], value['model'])
            elif value['type'] == 'codex': self.providers[name] = NativeCodexProvider(value['binary'], value['workspace'])
            elif value['type'] == 'command': self.providers[name] = CommandProvider(value['argv'], value['label'],value.get('log_file'))
            elif value['type'] == 'orchestrator': self.providers[name] = OrchestratorProvider(value)
            elif value['type'] == 'cloud': self.providers[name] = CloudProvider(value)
            elif value['type'] == 'mcp_events': self.providers[name] = DotProvider(value.get('agent_id','personal-dot'))
            elif value['type'] == 'mausbot': self.providers[name] = MausBotProvider(value, provider_id=name)
            else: raise ValueError('Unknown provider type')
        from hub_connections import HermesConnections
        self.hermes_connections = HermesConnections(self.store.directory, self.providers.get('hermes'))
        self.providers['hermes'] = self.hermes_connections
        for name, value in config['providers'].items():
            if value['type'] == 'routing': self.providers[name] = RoutingProvider(value, self.providers)
        self.coordinator = Coordinator(self.store, self.providers, self.coucou)
        self.fabric = self.coordinator.fabric
        from hub_mobile import MobileLink
        self.mobile=MobileLink(self); self.store.mobile_link=self.mobile
        from hub_awareness import Awareness
        self.awareness = Awareness(self.store, self.mobile.link)
        from hub_context_router import ContextRouter
        self.context_router = ContextRouter(self.store, config.get('context_router', {}))
        from desktop_speech_capture import DesktopSpeechCapture
        self.speech_capture=DesktopSpeechCapture(self)
        self.speech_capture.finalize_history()
        from speech_pipeline import SpeechPipeline
        self.speech_pipeline=SpeechPipeline(self)
        from hub_incidents import Incidents
        self.incidents = Incidents(self.store,self.mobile.link,config)
        from hub_codex import CodexLink
        self.codex = CodexLink(self.store,self.incidents,config.get('codex_link'))
        self.coordinator.codex = self.codex
        self.mcp = McpEvents(self, config.get('mcp_events'))
        self.oauth = McpOAuth(self)
        self.lock = threading.RLock()
        from hub_updates import Updates
        self.updates = Updates(os.environ.get('HUB_UPDATE_DIR'))
        self.coordinator.admission_guard = self.updates.barrier
        self.cached_status, self.status_at = {}, 0
        # Resume request-bound observation after a restart, never a send.
        for row in self.store.reconcilable_tasks():
            provider = self.providers.get(row['provider'])
            if callable(getattr(provider, 'watch', None)):
                stop = threading.Event()
                self.coordinator.active[row['id']] = stop
                if not provider.watch(row, self.coordinator, stop):
                    self.coordinator.active.pop(row['id'], None)

    def coucou(self, row, kind, data):
        from coucou_events import wire_event, write_event, EVENTS
        if kind=='assistant.message': self.mobile.deliver(row,data.get('text',''))
        if kind not in EVENTS: return
        sock = self.config.get('coucou_socket')
        if not sock: return
        event = {'version':1, 'deviceId':self.device, 'conversationId':row['id'], 'kind':kind}
        if data.get('text'): event['text'] = data['text'][:4000]
        if data.get('tool'): event['tool'] = data['tool']
        success = write_event(sock, wire_event(event, 'assistant-desktop'))
        self.store.event('coucou.delivery', row['context_id'], row['id'], delivered=success, hook=kind)

    def statuses(self):
        with self.lock:
            if time.monotonic() - self.status_at > 10:
                self.cached_status = {name: provider.status() for name,provider in self.providers.items()}
                self.status_at = time.monotonic()
            return self.cached_status

    def client_focus(self, client):
        focus = self.store.focus(client)
        if focus['context_id'] is None:
            try: focus = self.store.switch(client, self.store.setting('default-context',self.store.contexts()[0]['id']), focus['revision'])
            except Conflict: focus = self.store.focus(client)
        return focus

class HubHandler(BaseHTTPRequestHandler):
    server_version = 'AssistantHub/0.1'
    protocol_version = 'HTTP/1.1'

    @property
    def app(self): return self.server.app

    def log_message(self, *_): pass  # Never log URLs, prompts, tickets or headers.

    def check_origin(self):
        host = self.server.server_address[0]
        allowed = {authority(host, self.server.server_port)}
        if bind_ip(host).is_loopback:
            allowed.add(f'localhost:{self.server.server_port}')
        if hasattr(self.server, 'app'):
            allowed.update(http_origins(self.app.config))
        self.public_request = False
        self.auth_peer = self.client_address[0] if hasattr(self, 'client_address') else host
        proxy = self.app.proxy if hasattr(self.server, 'app') else ProxyOrigin({})
        if hasattr(self.headers, 'get_all'):
            for name in ('Host', 'Origin', 'Authorization', 'Cookie', 'X-Forwarded-Proto',
                         'X-Forwarded-For', 'X-Forwarded-Host', 'Forwarded', 'Content-Length'):
                if len(self.headers.get_all(name, [])) > 1:
                    raise PermissionError('Duplicate security header')
        if self.headers.get('Transfer-Encoding') is not None:
            raise PermissionError('Chunked requests are unsupported')
        supplied_host = self.headers.get('Host')
        forwarded = any(self.headers.get(h) is not None for h in
                        ('X-Forwarded-Proto', 'X-Forwarded-Host', 'X-Forwarded-For', 'Forwarded'))
        if supplied_host == proxy.host and proxy.host is not None:
            if not proxy.trusted(self.auth_peer) or self.headers.get('X-Forwarded-Proto') != 'https':
                raise PermissionError('HTTPS proxy required')
            forwarded_host = self.headers.get('X-Forwarded-Host')
            if forwarded_host is not None and forwarded_host != proxy.host:
                raise PermissionError('Invalid forwarded host')
            # One address only: HAProxy must overwrite, not append, this header.
            forwarded_peer = self.headers.get('X-Forwarded-For')
            if forwarded_peer:
                self.auth_peer = str(ipaddress.ip_address(forwarded_peer))
            self.public_request = True
            self.request_origin = proxy.origin
        else:
            if supplied_host not in allowed or forwarded:
                raise PermissionError('Invalid host or untrusted proxy headers')
            self.request_origin = 'http://' + supplied_host
        origin = self.headers.get('Origin')
        if origin and origin != self.request_origin: raise PermissionError('Invalid origin')
        public_navigation = (getattr(self,'command',None)=='GET' and
            urllib.parse.urlsplit(getattr(self,'path','')).path in {'/','/oauth/authorize'} and
            self.headers.get('Sec-Fetch-Mode','navigate') == 'navigate')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' and not public_navigation: raise PermissionError('Invalid origin')

    def authorized(self):
        header = self.headers.get('Authorization')
        # An explicitly supplied wrong bearer must not fall back to a browser cookie.
        if header is not None:
            if not header.startswith('Bearer '): return None
            secret = header[7:]
            return self.app.auth.resolve(secret) or self.app.oauth.resolve(secret)
        try:
            c = SimpleCookie(self.headers.get('Cookie','')); value = c.get('hub_session')
            if self.command == 'POST' and self.headers.get('Origin') != self.request_origin:
                return None
            return self.app.browser.resolve(value.value, self.request_origin) if value else None
        except Exception: return None

    def session_cookie(self, principal):
        value = self.app.browser.session(principal, self.request_origin)
        return f'hub_session={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400' + ('; Secure' if self.public_request else '')

    def body(self):
        if self.headers.get('Transfer-Encoding'): raise ValueError('Chunked requests are unsupported')
        length = int(self.headers.get('Content-Length','0'))
        if not 0 < length <= 65536: raise ValueError('Invalid body size')
        result = json.loads(self.rfile.read(length))
        if not isinstance(result, dict): raise ValueError('Expected a JSON object')
        return result

    def form(self):
        if self.headers.get('Transfer-Encoding'):raise ValueError('Chunked forms are not supported')
        if self.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded':
            raise ValueError('Expected form encoding')
        length=int(self.headers.get('Content-Length','0'))
        if not 0<length<=8192:raise ValueError('Invalid form size')
        raw=self.rfile.read(length)
        if len(raw)!=length:raise ValueError('Incomplete form')
        values=urllib.parse.parse_qs(raw.decode('utf-8'),keep_blank_values=True,max_num_fields=20)
        if any(len(items)!=1 for items in values.values()):raise ValueError('Duplicate form parameter')
        return {key:items[0] for key,items in values.items()}

    def reply(self, value, status=200, headers=None, mime='application/json; charset=utf-8'):
        body = value if isinstance(value, bytes) else encoded(value).encode('utf-8')
        self.send_response(status)
        for key,val in {'Content-Type':mime, 'Content-Length':str(len(body)), 'Cache-Control':'no-store',
                        'X-Content-Type-Options':'nosniff', 'Referrer-Policy':'no-referrer',
                        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
                        **(headers or {})}.items(): self.send_header(key,val)
        self.end_headers(); self.wfile.write(body)

    def do_GET(self): self.dispatch('GET')
    def do_POST(self): self.dispatch('POST')

    def dispatch(self, method):
        try:
            self.connection.settimeout(30)
            self.check_origin()
            url = urllib.parse.urlsplit(self.path)
            if url.scheme or url.netloc: raise ValueError('Expected a relative request path')
            path, query = url.path, urllib.parse.parse_qs(url.query)
            get = lambda key, default=None: query.get(key,[default])[0]
            if path in {'/.well-known/oauth-authorization-server','/.well-known/oauth-protected-resource','/.well-known/oauth-protected-resource/mcp'} and method=='GET':
                return self.reply(self.app.oauth.metadata(resource='oauth-protected-resource' in path))
            if path=='/oauth/register' and method=='POST':
                return self.reply(self.app.oauth.register(self.body()),201)
            if path=='/oauth/token' and method=='POST':
                return self.reply(self.app.oauth.exchange(self.form()))
            if path=='/oauth/authorize':
                if method=='GET':
                    if any(len(items)!=1 for items in query.values()):raise ValueError('Duplicate OAuth parameter')
                    page,nonce=self.app.oauth.consent({key:items[0] for key,items in query.items()})
                    cookie=f'hub_oauth_consent={nonce}; HttpOnly; SameSite=Lax; Path=/oauth/authorize; Max-Age=300'+('; Secure' if self.public_request else '')
                    return self.reply(page,headers={'Set-Cookie':cookie,'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"},mime='text/html; charset=utf-8')
                if self.headers.get('Origin')!=self.request_origin:raise PermissionError('Same-origin consent required')
                form=self.form();cookie=SimpleCookie(self.headers.get('Cookie',''));nonce=cookie.get('hub_oauth_consent')
                location=self.app.oauth.approve(form.get('nonce'),nonce.value if nonce else None,form.get('token'))
                return self.reply(b'',303,{'Location':location,'Set-Cookie':'hub_oauth_consent=; HttpOnly; SameSite=Lax; Path=/oauth/authorize; Max-Age=0'})
            if path in {'/awareness','/awareness.js','/awareness.css'} and method == 'GET':
                filename = {'/awareness':'awareness.html','/awareness.js':'awareness.js','/awareness.css':'awareness.css'}[path]
                mime = {'/awareness':'text/html','/awareness.js':'text/javascript','/awareness.css':'text/css'}[path]
                return self.reply((Path(__file__).parent/'hub_ui'/filename).read_bytes(), mime=mime+'; charset=utf-8')
            if path in {'/','/app.js','/app.css'} and method == 'GET':
                filename = {'/':'index.html','/app.js':'app.js','/app.css':'app.css'}[path]
                mime = {'/':'text/html','/app.js':'text/javascript','/app.css':'text/css'}[path]
                return self.reply((Path(__file__).parent/'hub_ui'/filename).read_bytes(), mime=mime+'; charset=utf-8')
            if path == '/api/login' and method == 'POST':
                body = self.body()
                if 'password' in body:
                    if self.headers.get('Origin') != self.request_origin:
                        raise Forbidden('Войдите с этой страницы Hub')
                    self.app.auth_failures.check_password_attempt(self.auth_peer)
                    principal = self.app.account.verify(body.get('username'), body.get('password'))
                    if not principal:
                        try: self.app.auth_failures.reject_password(self.auth_peer)
                        except RateLimited: raise
                        except PermissionError: raise PermissionError('Неверный логин или пароль')
                else:
                    principal = self.app.auth.resolve(body.get('token'))
                if not principal: self.app.auth_failures.reject(self.auth_peer)
                if self.app.auth.scope(principal)!='owner':raise Forbidden('Для входа нужен ключ владельца Hub')
                return self.reply({'ok':True},headers={'Set-Cookie':self.session_cookie(principal)})
            if path == '/api/auth/status' and method == 'GET':
                return self.reply({'configured': self.app.account.status()['configured']})
            if path == '/ticket' and method == 'GET':
                principal = self.app.browser.resolve(get('id',''), self.request_origin, ticket=True)
                if not principal: self.app.auth_failures.reject(self.auth_peer)
                return self.reply(b'',303,{'Location':'/', 'Set-Cookie':self.session_cookie(principal)})
            coucou_access=False
            prefix='/coucou/'+self.app.config.get('coucou_capability','unconfigured')
            if path in {prefix+'/v1/models',prefix+'/v1/chat/completions',prefix+'/api/speech-capture',prefix+'/api/desktop-hub'}:
                # Original Coucou's local provider sends a fixed "Bearer ollama".
                # The loopback capability exposes the model facade and explicit capture controls.
                coucou_access=(not self.public_request and bind_ip(self.server.server_address[0]).is_loopback
                               and self.auth_peer in ('127.0.0.1','::1')
                               and bool(self.app.config.get('coucou_capability'))
                               and self.headers.get('Authorization')=='Bearer ollama')
                path=path[len(prefix):]
            principal = self.authorized()
            if not principal and not coucou_access: self.app.auth_failures.reject(self.auth_peer)
            if self.app.updates.barrier() and method == 'POST' and path != '/api/logout':
                raise Conflict('Hub переключает версию. Повторите запрос после перезапуска.')
            if path.startswith('/api/releases'):
                account_owner = principal and principal.startswith('account:') and self.app.account.active(principal)
                publisher = principal and self.app.auth.scope(principal) == 'deploy'
                if not account_owner and not (publisher and path == '/api/releases/stage'):
                    raise Forbidden('Обновлениями управляет владелец; ключ публикации может только загрузить пакет')
                if path == '/api/releases' and method == 'GET': return self.reply(self.app.updates.status())
                if path == '/api/releases/stage' and method == 'POST':
                    from hub_updates import MAX_ARCHIVE
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 1 <= size <= MAX_ARCHIVE or self.headers.get('Content-Type') != 'application/gzip':
                        raise ValueError('Загрузите пакет application/gzip размером до 4 МиБ')
                    raw = self.rfile.read(size)
                    if len(raw) != size: raise ValueError('Пакет получен не полностью')
                    return self.reply(self.app.updates.stage(raw,self.headers.get('X-Release-SHA256')),201)
                if path == '/api/releases/activate' and method == 'POST':
                    body = self.body()
                    with self.app.coordinator.lock:
                        if self.app.coordinator.active: raise Conflict('Дождитесь завершения текущих задач перед обновлением')
                        result = self.app.updates.request(body['version'])
                    return self.reply(result,202)
                return self.reply({'error':'Unknown release endpoint'},404)
            if principal and self.app.auth.scope(principal) == 'deploy':
                raise Forbidden('Ключ публикации может только загружать релизы Hub')
            if path == '/api/logout' and method == 'POST':
                cookie = SimpleCookie(self.headers.get('Cookie','')); value = cookie.get('hub_session')
                if value: self.app.browser.revoke(value.value)
                return self.reply({'ok':True},headers={'Set-Cookie':'hub_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'+('; Secure' if self.public_request else '')})
            if path.startswith('/api/connections'):
                if not (principal and principal.startswith('account:') and self.app.account.active(principal)):
                    raise Forbidden('Для настройки подключений войдите по логину и паролю владельца')
                connections = self.app.hermes_connections
                if path == '/api/connections' and method == 'GET': return self.reply(connections.snapshot())
                if method == 'POST':
                    body = self.body()
                    if path == '/api/connections/test': return self.reply(connections.save(body, save=False))
                    if path == '/api/connections/save': return self.reply(connections.save(body))
                    if path == '/api/connections/activate':
                        result = connections.activate(body)
                        self.app.status_at = 0
                        return self.reply(result)
                return self.reply({'error':'Unknown connection endpoint'},404)
            if path in {'/api/account','/api/access-keys','/api/access-keys/revoke'}:
                account_owner = principal and principal.startswith('account:') and self.app.account.active(principal)
                bootstrap = (path == '/api/account' and not self.app.account.status()['configured'] and
                             principal and self.app.auth.scope(principal) == 'owner')
                if not account_owner and not bootstrap:
                    raise Forbidden('Для управления доступом войдите по логину и паролю')
                if path == '/api/account':
                    if method == 'GET': return self.reply(self.app.account.status())
                    self.app.auth_failures.check_password_attempt(self.auth_peer)
                    body = self.body()
                    try:
                        account = self.app.account.save(body.get('username'), body.get('password'), principal, body.get('current_password'))
                    except Forbidden:
                        try: self.app.auth_failures.reject_password(self.auth_peer)
                        except RateLimited: raise
                        except PermissionError: raise Forbidden('Неверный текущий пароль или устаревшая сессия')
                    return self.reply({'ok':True},headers={'Set-Cookie':self.session_cookie(account)})
                if path == '/api/access-keys' and method == 'GET':
                    return self.reply(self.app.auth.list())
                if method == 'POST':
                    body = self.body()
                    if path.endswith('/revoke'):
                        self.app.auth.revoke(body['id']); return self.reply({'ok':True})
                    scope = body.get('scope','app')
                    if scope not in {'app','webhook','mcp','deploy'}: raise ValueError('Недопустимые права приложения')
                    metadata, secret = self.app.auth.issue(body.get('name'), scope)
                    return self.reply({'key':metadata,'token':secret},201)
            if principal and self.app.auth.scope(principal)=='app' and path in {'/api/tickets','/ticket'}:
                raise Forbidden('Ключ приложения не предназначен для входа в панель')
            if principal and self.app.auth.scope(principal)=='webhook' and (method,path) not in {('POST','/webhooks/events'),('POST','/api/awareness/events')}:
                raise Forbidden('Этот ключ может только отправлять вебхуки')
            if principal and self.app.auth.scope(principal)=='mcp' and path!='/mcp':
                raise Forbidden('Этот ключ предназначен только для MCP-инструментов dot')
            if path.startswith('/api/awareness'):
                if not principal: raise Forbidden('Нужен ключ доступа к Hub')
                if method == 'POST' and path == '/api/awareness/events':
                    return self.reply(self.app.awareness.accept(self.body(), principal), 202)
                if self.app.auth.scope(principal) != 'owner': raise Forbidden('Нужен ключ владельца Hub')
                if method == 'GET' and path == '/api/awareness':
                    return self.reply({**self.app.awareness.snapshot(), 'routes':self.app.context_router.catalog()})
                if method == 'POST':
                    body = self.body()
                    if path == '/api/awareness/decision':
                        return self.reply(self.app.awareness.decide(body['id'], body['revision'], body['action'], body.get('seconds',3600)))
                    if path == '/api/awareness/publish':
                        return self.reply(self.app.awareness.publish(body['id'], body['revision'], body['device']))
                    if path == '/api/awareness/demo':
                        if not self.app.config.get('awareness_demo'): raise Forbidden('Демо выключено в конфигурации')
                        from hub_awareness import demo_events
                        for event in demo_events(): self.app.awareness.accept(event, 'synthetic-demo')
                        return self.reply(self.app.awareness.snapshot())
                return self.reply({'error':'Unknown awareness endpoint'},404)
            if path=='/mcp':
                self.app.mcp.require(principal)
                if method!='POST': return self.reply({'error':'Use MCP POST transport'},405,{'Allow':'POST'})
                result = self.app.mcp.rpc(principal,self.body())
                return self.reply(b'',202) if result is None else self.reply(result)
            if path=='/webhooks/events' and method=='POST':
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536:raise ValueError('Webhook payload must be 1..65536 bytes')
                raw=self.rfile.read(length)
                if len(raw)!=length:raise ValueError('Incomplete webhook payload')
                value=self.app.incidents.accept(raw,self.headers.get('Content-Type','application/json'),principal,
                    urllib.parse.unquote(self.headers.get('X-Event-Source','webhook'),errors='strict'),urllib.parse.unquote(self.headers.get('X-Event-Title','Новое событие'),errors='strict'),
                    self.headers.get('X-Event-Targets','auto'),self.headers.get('Idempotency-Key'))
                return self.reply({'id':value['id'],'status':value['status'],'targets':value['targets']},202)
            if path == '/api/desktop-hub':
                from hub_coucou import desktop_hub
                return self.reply(desktop_hub(self.app, method, self.body() if method == 'POST' else None))
            s = self.app.store
            if path.startswith('/link/'):
                return self.mobile_request(path,query,method)
            if method == 'GET':
                if path == '/api/speech-pipeline': return self.reply(self.app.speech_pipeline.status())
                if path == '/api/speech-capture': return self.reply(self.app.speech_capture.status())
                if path == '/api/speech-samples': return self.reply(self.app.mobile.speech.list(int(get('offset','0'))))
                if path == '/api/audio-notes': return self.reply(self.app.mobile.notes.read(get('query',''),get('id',''),int(get('offset','0'))))
                if path.startswith('/api/audio-notes/audio/'): return self.reply(self.app.mobile.notes.blob(path.rsplit('/',1)[1]),mime='audio/wav')
                if path == '/health': return self.reply({'status':'ok','version':'0.1.0','release_id':os.environ.get('HUB_RELEASE_ID')})
                if path == '/api/state':
                    client = text(get('client','desktop'),128)
                    focus = self.app.client_focus(client); context = focus['context_id']
                    memory = s.memory_page(context,limit=12)
                    return self.reply({'contexts':s.contexts(), 'focus':focus, 'providers':self.app.statuses(),
                        'history':s.history(context), 'memory':memory['items'], 'memoryHasMore':memory['next'] is not None,
                        'tasks':s.tasks(), 'chats':s.chats(limit=100),
                        'nodes':self.app.fabric.nodes(), 'timers':self.app.fabric.timers(context),
                        'calls':[{k:c[k] for k in ('id','node_id','capability','status','error')} for c in self.app.fabric.calls(context)],
                        'notifications':s.notifications()[-60:], 'coucou':{'socketConfigured':bool(self.app.config.get('coucou_socket'))}})
                if path == '/api/events': return self.reply(s.events(int(get('after','0')), get('context')))
                if path == '/api/incidents':return self.reply(self.app.incidents.list())
                if path == '/api/dot/status':return self.reply(self.app.mcp.status())
                if path == '/api/codex/chats':return self.reply(self.app.codex.list(get('query','')))
                if path == '/api/codex/status':return self.reply({'enabled':self.app.codex.enabled,'error':self.app.codex.last_error})
                if path == '/api/codex/commands':return self.reply(self.app.codex.commands(get('client','desktop'),uid(get('context'))))
                if path.startswith('/api/codex/chats/'):return self.reply(self.app.codex.report(path.rsplit('/',1)[1]))
                if path.startswith('/api/incidents/'):return self.reply(self.app.incidents.get(path.rsplit('/',1)[1]))
                if path == '/api/presence':return self.reply(self.app.incidents.clients())
                if path == '/api/context': return self.reply(s.packet(uid(get('id')), int(get('budget','24000')),query=get('query')))
                if path == '/api/memory':
                    cursor = [get('after_scope'),get('after_key')] if get('after_scope') is not None else None
                    return self.reply(s.memory_page(uid(get('context')),int(get('limit','50')),cursor))
                if path == '/api/history': return self.reply(s.history(uid(get('context')), int(get('before')) if get('before') else None))
                if path == '/api/nodes':return self.reply(self.app.fabric.nodes(get('query',''),schemas=get('schemas')=='1'))
                if path == '/api/tools':
                    row = s.task(uid(get('task')))
                    if row['context_id'] != uid(get('context')):raise ValueError('Task belongs to another topic')
                    return self.reply(self.app.coordinator.model_tools(row))
                if path.startswith('/api/capabilities/calls/'):
                    return self.reply(self.app.fabric.result(path.rsplit('/',1)[1],uid(get('context'))))
                if path == '/api/timers':return self.reply(self.app.fabric.timers(uid(get('context'))))
                if path.startswith('/api/tasks/'): return self.reply(s.task(path.rsplit('/',1)[-1]))
                if path == '/v1/models':
                    return self.reply({'object':'list','data':
                        [{'id':'assistant-'+p,'object':'model','owned_by':'assistant-hub'} for p in self.app.providers]+
                        [{'id':f'hub/{p}/{c["id"]}','object':'model','owned_by':'assistant-hub'}
                        for c in s.contexts() for p in self.app.providers]})
            else:
                body = self.body()
                if path == '/api/codex/acknowledge':
                    return self.reply(self.app.codex.acknowledge_unknown(body['id'],body['client'],body['context_id']))
                if path == '/api/codex/voice':
                    client=text(body['client'],128)
                    context=uid(body['context_id']) if body.get('context_id') else self.app.client_focus(client)['context_id']
                    with s.connect() as db:s.require_context(db,context)
                    reply=self.app.codex.voice(text(body['text'],2000),client,context,uid(body['id']))
                    if reply is None:raise ValueError('Use an explicit Codex command')
                    return self.reply({'id':body['id'],'reply':reply[:1500]})
                if path == '/api/speech-pipeline':return self.reply(self.app.speech_pipeline.start(body.get('action')))
                if path == '/api/speech-capture':
                    if body.get('action')=='start':return self.reply(self.app.speech_capture.start(body.get('sources',[]),body.get('transcribe',True)))
                    if body.get('action')=='stop':return self.reply(self.app.speech_capture.stop())
                    raise ValueError('Choose start or stop')
                if path == '/api/presence':return self.reply(self.app.incidents.presence(body['id'],body['kind'],body['label'],body['active'],body.get('idle_seconds',0)))
                if path == '/api/deliveries/claim':return self.reply(self.app.incidents.claim(body['client']))
                if path == '/api/deliveries/receipt':return self.reply(self.app.incidents.receipt(body['client'],body['id'],body['status'],body.get('lease'),body.get('coucou_written',False)))
                if path == '/api/tickets':
                    ticket = self.app.browser.ticket(principal, self.request_origin)
                    return self.reply({'path':'/ticket?id='+ticket})
                if path == '/api/contexts': return self.reply(s.create_context(body['title']),201)
                if path == '/api/focus': return self.reply(s.switch(body['client'],body['context_id'],body['revision']))
                if path == '/api/follow': return self.reply(s.follow_notification(body['client'],body['notification'],body['revision']))
                if path == '/api/memory':
                    value = s.put_memory(body['key'],body.get('body',''),'owner',body['revision'],body.get('context_id'),body.get('deleted',False))
                    s.event('memory.changed',body.get('context_id'),None,key=value['key'],scope=value['scope'],revision=value['revision'])
                    return self.reply(value)
                if path == '/api/messages':
                    row = self.app.coordinator.submit(body['context_id'],body['client'],body['provider'],body['prompt'],body['id'])
                    return self.reply(row,202)
                if path == '/api/nodes':
                    return self.reply(self.app.fabric.register(body['id'],body['label'],body['kind'],body['capabilities'],body.get('ttl',90)),201)
                if path.startswith('/api/nodes/'):
                    parts=path.split('/')
                    if len(parts)==5 and parts[4]=='claim':return self.reply(self.app.fabric.claim(parts[3]))
                    if len(parts)==5 and parts[4]=='result':
                        return self.reply(self.app.fabric.complete(parts[3],body['id'],body['lease'],body.get('result'),body.get('error','')))
                if path == '/api/capabilities/call':
                    return self.reply(self.app.fabric.call(body['node'],body['capability'],body['arguments'],body['context_id'],body['client'],body['id']),202)
                if path == '/api/cancel': return self.reply(self.app.coordinator.cancel(body['id']))
                if path == '/api/tool':
                    context = uid(body['context_id'])
                    with s.connect() as db: s.require_context(db,context)
                    row = {'id':uid(body['request_id']), 'context_id':context, 'client':text(body['client'],128), 'provider':'mcp'}
                    if body.get('task_id'):
                        parent = s.task(uid(body['task_id']))
                        if parent['context_id'] != context:
                            raise ValueError('Task belongs to another topic')
                        row = parent
                    # MCP tools expose no agent launching; task launches go through the task API.
                    if body['name'] == 'agent_task': raise ValueError('Use the owned task API for agent launches')
                    return self.reply(self.app.coordinator.call_tool(body['name'],body['arguments'],row))
                if path == '/api/notify': return self.reply(s.notify(body['context_id'],text(body['source'],256),body['body']))
                if path == '/v1/chat/completions': return self.completion(body)
                if path == '/conversation': return self.reply(self.app.mobile.turn(body))
            self.reply({'error':'Unknown endpoint'},404)
        except Conflict as error: self.reply({'error':str(error)},409)
        except OAuthError as error: self.reply({'error':str(error)},400)
        except Forbidden as error:
            self.close_connection=True
            self.reply({'error':str(error)},403,{'Connection':'close'})
        except RateLimited as error:
            self.close_connection = True
            self.reply({'error':str(error)},429,{'Retry-After':'60','Connection':'close'})
        except PermissionError as error:
            self.close_connection = True
            challenge='Bearer realm="assistant-hub"'
            if urllib.parse.urlsplit(self.path).path=='/mcp' and self.app.oauth.enabled():
                challenge+=', resource_metadata="'+self.app.oauth.issuer+'/.well-known/oauth-protected-resource/mcp"'
            self.reply({'error':str(error)},401,{'WWW-Authenticate':challenge,'Connection':'close'})
        except (ValueError, KeyError, TypeError) as error:
            self.close_connection = True
            self.reply({'error':str(error)[:500]},400,{'Connection':'close'})
        except (BrokenPipeError, ConnectionResetError, TimeoutError): pass
        except Exception: self.reply({'error':'Hub request failed; inspect the local runtime'},500)

    def mobile_request(self,path,query,method):
        from home_link import MAX_IMAGE,MAX_SNAPSHOT,RevisionConflict
        from audio_notes import MAX_AUDIO
        parts=path.split('/');device=uid(parts[2]);route=parts[3:];link=self.app.mobile.link
        try:
            if method=='GET':
                if route==['speech-samples']:return self.reply(self.app.mobile.speech.list(int(query.get('offset',['0'])[0]),3))
                if route==['audio-notes']:return self.reply(self.app.mobile.notes.read(offset=int(query.get('offset',['0'])[0]),limit=3))
                if len(route)==2 and route[0]=='audio':return self.reply(self.app.mobile.notes.blob(route[1]),mime='audio/wav')
                if route==['knowledge']:return self.reply(link.knowledge(device))
                if route==['events']:
                    after=int(query.get('after',['0'])[0]);wait=int(query.get('wait',['0'])[0])
                    return self.reply(link.events(device,after,wait))
                if len(route)==2 and route[0]=='image':return self.reply(link.image(device,route[1]),mime='application/octet-stream')
            else:
                size=int(self.headers.get('Content-Length','0'));image=len(route)==2 and route[0]=='image'
                audio=len(route)==2 and route[0] in ('audio','speech-audio')
                if self.headers.get('Transfer-Encoding') or not 0<size<=(MAX_AUDIO if audio else MAX_IMAGE if image else MAX_SNAPSHOT if route==['knowledge'] else 65536 if route in (['audio-notes'],['speech-samples']) else 16384):raise ValueError('Invalid link size')
                raw=self.rfile.read(size)
                if len(raw)!=size:raise ValueError('Incomplete request')
                if audio:return self.reply((self.app.mobile.speech.audio if route[0]=='speech-audio' else self.app.mobile.notes).blob(route[1],raw))
                if image:return self.reply(link.image(device,route[1],raw))
                body=json.loads(raw)
                if route==['speech-samples']:return self.reply(self.app.mobile.speech.put(device,body))
                if route==['audio-notes']:return self.reply(self.app.mobile.audio_note(device,body))
                if route==['knowledge']:return self.reply(self.app.mobile.sync(device,body))
                if route==['events']:return self.reply(link.publish(device,body))
                if route==['ack'] and set(body)=={'through'}:return self.reply(link.ack(device,body['through']))
                if route==['presence']:return self.reply(self.app.incidents.presence('android:'+device,'phone','Android',body['active'],body.get('idle_seconds',0)))
                if route==['receipt']:return self.reply(self.app.incidents.receipt('android:'+device,body['id'],body['status']))
            self.reply({'error':'Unknown link endpoint'},404)
        except RevisionConflict as error:self.reply({'error':'revision_conflict','revision':error.revision},409)
        except KeyError:self.reply({'error':'Unknown paired source/device'},404)

    def completion(self, body):
        parts = body.get('model','').split('/')
        if body.get('model','').startswith('assistant-'):
            provider=body['model'].removeprefix('assistant-');context=self.app.client_focus('coucou')['context_id']
        elif len(parts)==3 and parts[0]=='hub':provider,context=parts[1],uid(parts[2])
        else: raise ValueError('Choose an assistant-provider or hub/provider/topic model from /v1/models')
        history = body.get('messages', [])
        if not history or history[-1].get('role') != 'user' or not isinstance(history[-1].get('content'),str):
            raise ValueError('The last message must contain user text')
        task_id = self.headers.get('X-Request-ID') or str(uuid.uuid4())
        row = self.app.coordinator.submit(context,'coucou',provider,history[-1]['content'],uid(task_id))
        stream = body.get('stream',False)
        if stream:
            self.send_response(200)
            for k,v in {'Content-Type':'text/event-stream', 'Cache-Control':'no-store', 'Connection':'close'}.items(): self.send_header(k,v)
            self.end_headers(); self.close_connection=True
        deadline, sent = time.monotonic()+260, ''
        while time.monotonic()<deadline:
            row = self.app.store.task(row['id'])
            if stream and row['output'].startswith(sent) and row['output'] != sent:
                value = {'id':row['id'],'object':'chat.completion.chunk','choices':[{'index':0,'delta':{'content':row['output'][len(sent):]},'finish_reason':None}]}
                self.wfile.write(('data: '+encoded(value)+'\n\n').encode()); self.wfile.flush(); sent=row['output']
            if row['status'] in TERMINAL: break
            if stream: self.wfile.write(b': waiting\n\n'); self.wfile.flush()
            time.sleep(0.2)
        content = row['output'] if row['status'] == 'completed' else row['error'] or 'Превышено время ожидания. Статус задачи доступен в Hub.'
        if stream:
            remainder = content[len(sent):] if content.startswith(sent) else '\n'+content
            value = {'id':row['id'],'object':'chat.completion.chunk','choices':[{'index':0,'delta':{'content':remainder},'finish_reason':'stop'}]}
            self.wfile.write(('data: '+encoded(value)+'\n\ndata: [DONE]\n\n').encode()); self.wfile.flush()
        else:
            self.reply({'id':row['id'],'object':'chat.completion','model':body['model'],
                        'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}]})


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--config',required=True)
    args = parser.parse_args()
    config = private_json(args.config)
    host = bind_ip(config.get('host','127.0.0.1'))
    os.umask(0o077)
    app = HubApplication(config)
    app.fabric.start()
    app.incidents.start()
    app.codex.start()
    app.mcp.start()
    server_type = ThreadingHTTPServer
    if host.version == 6:
        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_type = IPv6Server
    server = server_type((str(host),config.get('port',8778)),HubHandler); server.app = app
    server.daemon_threads = True
    print('Assistant Hub ready on http://'+authority(str(host),server.server_port),flush=True)
    import signal
    def shutdown(*_):
        for row in app.store.tasks():
            if row['status'] not in TERMINAL and not getattr(app.providers.get(row['provider']),'deferred',False): app.coordinator.cancel(row['id'])
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown)
    try: server.serve_forever()
    finally:
        app.codex.close()
        app.mcp.close()
        app.fabric.close()
        app.incidents.close()
        server.server_close()
        deadline=time.monotonic()+4
        while app.coordinator.active and time.monotonic()<deadline:time.sleep(.05)


if __name__ == '__main__': main()
