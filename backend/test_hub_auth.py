"""Real HTTP authorization/proxy tests with synthetic keys and a temporary DB."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from agent_hub import HubStore
from hub_auth import AccessKeys, AuthFailures, BrowserCredentials, MAX_BROWSER_CREDENTIALS, RateLimited
from hub_network import ProxyOrigin
from hub_server import HubApplication, HubHandler


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HubStore(Path(self.tmp.name)/'state')
        self.owner = secrets.token_urlsafe(48)
        self.keys = AccessKeys(self.store, self.owner)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_digest_persisted_key_revocation_survives_restart(self):
        row, secret = self.keys.issue('Tablet')
        self.assertEqual(self.keys.resolve(secret), row['id'])
        self.assertEqual(self.keys.resolve(self.owner), 'owner')
        self.assertIsNone(self.keys.resolve('x'*64))
        self.assertIsNone(self.keys.resolve('ю'*40))
        self.assertEqual(self.keys.list(), [row])
        with self.store.connect() as db:
            saved = dict(db.execute('SELECT * FROM api_keys').fetchone())
        self.assertNotIn(secret, json.dumps(saved))
        self.assertNotIn('digest', self.keys.list()[0])
        self.keys.revoke(row['id'])
        self.keys.revoke(row['id'])
        reopened = AccessKeys(HubStore(self.store.directory), self.owner)
        self.assertIsNone(reopened.resolve(secret))
        self.assertIsNotNone(reopened.list()[0]['revoked_at'])
        self.assertEqual(reopened.resolve(self.owner), 'owner')

    def test_browser_credentials_expire_are_bounded_and_follow_key_revocation(self):
        row, secret = self.keys.issue('Browser')
        browser = BrowserCredentials(self.keys)
        origin = 'https://hub.example.com'
        session = browser.session(row['id'], origin)
        ticket = browser.ticket(row['id'], origin)
        self.assertIsNone(browser.resolve(session, 'http://127.0.0.1:8778'))
        self.assertEqual(browser.resolve(ticket, origin, ticket=True), row['id'])
        self.assertIsNone(browser.resolve(ticket, origin, ticket=True))
        self.keys.revoke(row['id'])
        self.assertIsNone(browser.resolve(session, origin))
        with patch('hub_auth.time.time', return_value=100):
            session = browser.session('owner', origin)
            ticket = browser.ticket('owner', origin)
        with patch('hub_auth.time.time', return_value=86500):
            self.assertIsNone(browser.resolve(session, origin))
            self.assertIsNone(browser.resolve(ticket, origin, ticket=True))
        for _ in range(MAX_BROWSER_CREDENTIALS+5):
            browser.session('owner', origin)
            browser.ticket('owner', origin)
        self.assertEqual(len(browser.sessions), MAX_BROWSER_CREDENTIALS)
        self.assertEqual(len(browser.tickets), MAX_BROWSER_CREDENTIALS)

    def test_cli_secret_file_and_no_overwrite(self):
        root = Path(self.tmp.name)
        config = root/'config.json'
        config.write_text(json.dumps({'state':str(self.store.directory)}))
        config.chmod(0o600)
        script = Path(__file__).resolve().parent.parent/'tools/hub_keys.py'
        output = root/'tablet.key'
        command = [sys.executable,str(script),'--config',str(config)]
        result = subprocess.run(command+['create','Tablet','--output',str(output)],capture_output=True,text=True,check=True)
        row = json.loads(result.stdout)
        secret = output.read_text().strip()
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(secret, result.stdout+result.stderr)
        self.assertEqual(self.keys.resolve(secret), row['id'])
        denied = subprocess.run(command+['create','Other','--output',str(output)],capture_output=True,text=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(output.read_text().strip(), secret)
        listed = subprocess.run(command+['list'],capture_output=True,text=True,check=True)
        self.assertNotIn(secret, listed.stdout)
        self.assertEqual(len(json.loads(listed.stdout)), 1)
        subprocess.run(command+['revoke',row['id']],capture_output=True,check=True)
        self.assertIsNone(self.keys.resolve(secret))
        link = root/'link.key'
        link.symlink_to(output)
        denied = subprocess.run(command+['create','Symlink','--output',str(link)],capture_output=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(output.read_text().strip(), secret)

    def test_cli_rolls_back_key_if_output_write_fails(self):
        path = Path(__file__).resolve().parent.parent/'tools/hub_keys.py'
        spec = importlib.util.spec_from_file_location('test_key_cli', path)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        output = Path(self.tmp.name)/'broken.key'
        with patch.object(cli.os, 'fsync', side_effect=OSError('Fixture failure')):
            with self.assertRaises(OSError):cli.create(self.keys, 'Broken', output)
        self.assertFalse(output.exists())
        self.assertIsNotNone(self.keys.list()[0]['revoked_at'])

    def test_failed_auth_windows_are_bounded_and_expire(self):
        limiter = AuthFailures()
        with patch('hub_auth.time.monotonic', return_value=1):
            for _ in range(20):
                with self.assertRaises(PermissionError) as error:limiter.reject('peer')
                self.assertNotIsInstance(error.exception, RateLimited)
            with self.assertRaises(RateLimited):limiter.reject('peer')
            for i in range(1500):
                with self.assertRaises(PermissionError):limiter.reject(str(i))
            self.assertEqual(len(limiter.peers), 1024)
        with patch('hub_auth.time.monotonic', return_value=61):
            with self.assertRaises(PermissionError) as error:limiter.reject('peer')
            self.assertNotIsInstance(error.exception, RateLimited)


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.owner = secrets.token_urlsafe(48)
        token = root/'token'
        token.write_text(self.owner)
        token.chmod(0o600)
        self.config = {'state':str(root/'state'), 'token_file':str(token), 'providers':{},
                       'public_url':'https://hub.example.com', 'trusted_proxies':['127.0.0.1'],
                       'coucou_capability':'fixture-private-coucou-capability'}
        self.app = HubApplication(self.config)
        self.row, self.key = self.app.auth.issue('Test client')
        self.server = ThreadingHTTPServer(('127.0.0.1',0), HubHandler)
        self.server.app = self.app
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path='/health', key=None, body=None, public=True, headers=None):
        h = {'Content-Type':'application/json'}
        if public:
            h.update({'Host':'hub.example.com','X-Forwarded-Proto':'https','X-Forwarded-For':'203.0.113.5'})
        if key is not None:h['Authorization'] = 'Bearer '+key
        h.update(headers or {})
        client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        client.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None, h)
        response = client.getresponse()
        data, status, result_headers = response.read(), response.status, dict(response.getheaders())
        client.close()
        return status, result_headers, data

    def test_all_data_routes_reject_missing_wrong_and_query_keys(self):
        for key in (None, 'wrong'):
            for path in ('/health','/api/state','/api/memory','/api/events','/api/context','/api/nodes','/api/tools',
                         '/api/tasks/fake','/api/timers','/v1/models','/link/fake/knowledge'):
                status, headers, data = self.request(path, key)
                self.assertIn(status, (401,429))
                if status == 401:self.assertEqual(headers['WWW-Authenticate'], 'Bearer realm="assistant-hub"')
                self.assertNotIn(self.owner.encode(), data)
        for path in ('/api/messages','/api/tool','/api/notify','/conversation','/api/capabilities/call','/api/nodes'):
            self.assertIn(self.request(path, body={})[0], (401,429))
        self.assertIn(self.request('/health?token='+self.key)[0], (401,429))
        self.assertEqual(self.request(key=self.key)[0], 200)
        self.assertEqual(self.request(key=self.owner)[0], 200)
        self.assertIn(self.request('/api/login',body={'token':'ю'*40})[0], (401,429))

    def test_issued_key_authorizes_data_then_immediate_revocation(self):
        self.assertEqual(self.request('/api/state', self.key)[0], 200)
        self.assertEqual(self.request('/api/contexts', self.key, {'title':'Fixture topic'})[0], 201)
        self.app.auth.revoke(self.row['id'])
        self.assertEqual(self.request(key=self.key)[0], 401)
        self.assertEqual(self.request(key=self.owner)[0], 200)

    def test_only_configured_https_proxy_and_exact_origin_allowed(self):
        for headers in ({'X-Forwarded-Proto':'http'}, {'X-Forwarded-Proto':''}, {'Host':'evil.example.com'},
                        {'Origin':'https://evil.example.com'}, {'Origin':'http://hub.example.com'},
                        {'X-Forwarded-Host':'evil.example.com'}, {'X-Forwarded-For':'203.0.113.1, 203.0.113.2'},
                        {'Sec-Fetch-Site':'cross-site'}):
            self.assertIn(self.request(key=self.key, headers=headers)[0], (400,401))
        self.assertEqual(self.request(key=self.key,headers={'Origin':self.config['public_url']})[0], 200)
        self.app.proxy = ProxyOrigin({**self.config, 'trusted_proxies':['10.42.0.2']})
        self.assertEqual(self.request(key=self.key)[0], 401)
        self.assertEqual(self.request(key=self.key,public=False)[0], 200)
        self.assertEqual(self.request(key=self.key,public=False,headers={'X-Forwarded-Proto':'https'})[0], 401)

    def test_public_cookie_secure_origin_bound_csrf_checked_and_revoked(self):
        status, headers, _ = self.request('/api/login', body={'token':self.key})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie']
        for flag in ('Secure', 'HttpOnly', 'SameSite=Strict'):self.assertIn(flag, cookie)
        h = {'Cookie':cookie.split(';')[0]}
        self.assertEqual(self.request('/api/state', headers=h)[0], 200)
        self.assertEqual(self.request('/api/state', public=False, headers=h)[0], 401)
        self.assertEqual(self.request('/api/state', key='wrong', headers=h)[0], 401)
        self.assertEqual(self.request('/api/state', headers={**h,'Authorization':''})[0], 401)
        self.assertEqual(self.request('/api/contexts', body={'title':'No origin'}, headers=h)[0], 401)
        self.assertEqual(self.request('/api/contexts', body={'title':'Same origin'},
                         headers={**h,'Origin':'https://hub.example.com'})[0], 201)
        self.assertEqual(self.request('/api/contexts', body={'title':'Evil origin'},
                         headers={**h,'Origin':'https://evil.example.com'})[0], 401)
        self.app.auth.revoke(self.row['id'])
        self.assertEqual(self.request('/api/state', headers=h)[0], 401)

    def test_tickets_are_origin_bound_single_use_and_revoke_with_key(self):
        status, _, body = self.request('/api/tickets', self.key, {})
        self.assertEqual(status, 200)
        path = json.loads(body)['path']
        status, headers, _ = self.request(path)
        self.assertEqual(status, 303)
        self.assertIn('Secure', headers['Set-Cookie'])
        self.assertEqual(self.request(path)[0], 401)
        path = json.loads(self.request('/api/tickets', self.key, {})[2])['path']
        self.assertEqual(self.request(path, public=False)[0], 401)
        path = json.loads(self.request('/api/tickets', self.key, {})[2])['path']
        self.app.auth.revoke(self.row['id'])
        self.assertEqual(self.request(path)[0], 401)

    def test_coucou_fixed_bearer_only_works_on_direct_loopback(self):
        path = '/coucou/fixture-private-coucou-capability/v1/models'
        self.assertEqual(self.request(path, key='ollama', public=False)[0], 200)
        self.assertEqual(self.request(path, key='ollama')[0], 401)
        self.assertEqual(self.request('/api/state', key='ollama', public=False)[0], 401)
        self.assertEqual(self.request('/v1/models', key=self.key)[0], 200)

    def test_login_page_has_no_private_data_and_failed_auth_is_limited(self):
        status, _, data = self.request('/')
        self.assertEqual(status, 200)
        self.assertNotIn(self.owner.encode(), data)
        self.assertNotIn(self.key.encode(), data)
        for _ in range(20):self.assertEqual(self.request(key='bad')[0], 401)
        status, headers, _ = self.request(key='bad')
        self.assertEqual(status, 429)
        self.assertEqual(headers['Retry-After'], '60')
        self.assertEqual(self.request(key=self.key)[0], 200)
        self.assertEqual(self.request('/api/login', body={'token':self.key})[0], 200)

    def test_duplicate_authentication_headers_cannot_override_host_or_key(self):
        for name, value in (('Host','evil.example.com'),('Authorization','Bearer wrong'),
                            ('X-Forwarded-Proto','http'),('Origin','https://evil.example.com')):
            client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
            client.putrequest('GET', '/health', skip_host=True)
            for header, item in {'Host':'hub.example.com','X-Forwarded-Proto':'https',
                                 'Origin':'https://hub.example.com','Authorization':'Bearer '+self.key}.items():
                client.putheader(header, item)
            client.putheader(name, value)
            client.endheaders()
            response = client.getresponse()
            response.read()
            self.assertEqual(response.status, 401)
            client.close()

    def test_ambiguous_request_framing_is_rejected_before_authentication(self):
        for extra in ((('Content-Length','0'),('Content-Length','4')), (('Transfer-Encoding','chunked'),)):
            client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
            client.putrequest('POST', '/api/login', skip_host=True)
            client.putheader('Host', 'hub.example.com')
            client.putheader('X-Forwarded-Proto', 'https')
            for name, value in extra:client.putheader(name, value)
            client.endheaders()
            response = client.getresponse()
            response.read()
            self.assertEqual(response.status, 401)
            self.assertEqual(response.getheader('Connection'), 'close')
            client.close()


if __name__ == '__main__':unittest.main()
