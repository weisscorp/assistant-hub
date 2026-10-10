"""Synthetic MCP Events / OAuth / device acknowledgements. No real cloud or devices."""
import base64
import hashlib
import hmac
import io
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.parse
import uuid
from agent_hub import Conflict, encoded
from hub_mcp_events import EVENT, VERSION, McpEvents, CallbackError, PinnedHTTPS, callback_url, public_addresses, signed_headers, webhook_post
from hub_mcp_oauth import OAuthError, REDIRECT
from hub_dot_stdio import run as relay
from hub_server import HubApplication, HubHandler


def uid():
    return str(uuid.uuid4())


class DotFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        token = root/'token'
        self.owner = secrets.token_urlsafe(48)
        token.write_text(self.owner); token.chmod(0o600)
        self.config = {'state':str(root/'state'), 'token_file':str(token), 'providers':
            {'dot':{'type':'mcp_events', 'agent_id':'personal-dot'}}, 'mcp_events':{'request_timeout':300},
            'public_url':'https://hub.example.com', 'trusted_proxies':['127.0.0.1']}
        self.app = HubApplication(self.config)
        self.context = self.app.store.contexts()[0]['id']
        self.key_row, self.key = self.app.auth.issue('Synthetic dot', 'mcp')
        self.web_row, self.web_key = self.app.auth.issue('Synthetic webhook', 'webhook')
        self.secret = 'whsec_' + base64.b64encode(b's'*32).decode()
        self.sent = []
        self.app.mcp.sender = self.send

    def tearDown(self):
        self.app.mcp.close()
        self.tmp.cleanup()

    def send(self, url, body, headers):
        self.sent.append((url, body, headers))
        value = json.loads(body)
        if value.get('type') == 'verification':
            return 200, encoded({'challenge':value['challenge']}).encode()
        return 202, b'{}'

    def subscribe(self, context=None, principal=None, secret=None):
        args = {'agent_id':'personal-dot'}
        if context is not None: args['context_id'] = context
        params = {'name':EVENT, 'arguments':args, 'delivery':{'mode':'webhook',
                  'url':'https://callback.example.com/events', 'secret':secret or self.secret}, 'cursor':None}
        return self.app.mcp.subscribe(principal or self.key_row['id'], params), params

    def request(self, prompt='Какая погода в тестовом городе?', context=None):
        row = self.app.coordinator.submit(context or self.context, 'fixture-client', 'dot', prompt, uid())
        end = time.monotonic()+3
        while time.monotonic() < end:
            with self.app.store.connect() as db:
                ready = db.execute('SELECT 1 FROM dot_requests WHERE id=?', (row['id'],)).fetchone()
            if ready: return self.app.store.task(row['id'])
            time.sleep(.01)
        self.fail('Dot request did not enter the durable queue')

    def tool(self, name, row, **args):
        return self.app.mcp.call_tool(self.key_row['id'], name, {'request_id':row['id'], **args})


class EventsTests(DotFixture):
    def test_discovery_and_tools_follow_documented_protocol(self):
        def rpc(method, params=None):
            return self.app.mcp.rpc(self.key_row['id'], {'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
        discovery = rpc('server/discover')['result']
        self.assertEqual(discovery['supportedVersions'], [VERSION])
        self.assertIn('events', discovery['capabilities'])
        events = rpc('events/list')['result']['events']
        self.assertEqual(events[0]['name'], EVENT)
        tools = rpc('tools/list')['result']['tools']
        self.assertIn('submit_result', {t['name'] for t in tools})
        self.assertNotIn('agent_task', {t['name'] for t in tools})
        self.assertNotIn('context_switch', {t['name'] for t in tools})
        self.assertTrue(next(t for t in tools if t['name']=='get_request')['annotations']['readOnlyHint'])

    def test_standard_webhooks_signature_exact_bytes_and_id(self):
        body = '{"text":"Русский текст"}'.encode()
        with patch('hub_mcp_events.time.time', return_value=1780000000):
            headers = signed_headers('evt_123', body, self.secret, 'sub_123')
        signature = base64.b64encode(hmac.new(b's'*32, b'evt_123.1780000000.'+body, hashlib.sha256).digest()).decode()
        self.assertEqual(headers['webhook-signature'], 'v1,'+signature)
        self.assertEqual(headers['webhook-id'], 'evt_123')
        self.assertEqual(headers['X-MCP-Subscription-Id'], 'sub_123')

    def test_jsonrpc_notifications_cannot_run_mutations_without_an_id(self):
        _, params = self.subscribe()
        with self.app.store.connect() as db:
            before = db.execute('SELECT active FROM mcp_subscriptions').fetchone()[0]
        result = self.app.mcp.rpc(self.key_row['id'], {'jsonrpc':'2.0','method':'events/unsubscribe','params':params})
        self.assertEqual(result['error']['code'], -32600)
        with self.app.store.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM mcp_subscriptions').fetchone()[0], before)

    def test_subscribe_idempotence_and_secret_rotation(self):
        first, params = self.subscribe(self.context)
        reordered = {**params, 'arguments':{'context_id':self.context, 'agent_id':'personal-dot'}}
        second = self.app.mcp.subscribe(self.key_row['id'], reordered)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.sent), 1)
        new_secret = 'whsec_' + base64.b64encode(b'n'*32).decode()
        third, _ = self.subscribe(self.context, secret=new_secret)
        self.assertEqual(first['id'], third['id'])
        self.assertEqual(len(self.sent), 2)
        with self.app.store.connect() as db:
            row = db.execute('SELECT * FROM mcp_subscriptions').fetchone()
        self.assertEqual(row['previous_secret'], self.secret)
        self.assertEqual(row['secret'], new_secret)
        self.request(); self.app.mcp.queue_pending(); self.app.mcp.process_one()
        self.assertEqual(len(self.sent[-1][2]['webhook-signature'].split()), 2)

    def test_verification_failure_never_activates_and_has_protocol_error(self):
        _, params = self.subscribe()
        self.app.mcp.unsubscribe(self.key_row['id'], params)
        params['delivery']['secret'] = 'whsec_' + base64.b64encode(b'x'*32).decode()
        self.app.mcp.sender = lambda *_: (200, b'{"challenge":"wrong"}')
        reply = self.app.mcp.rpc(self.key_row['id'], {'jsonrpc':'2.0','id':2,'method':'events/subscribe','params':params})
        self.assertEqual(reply['error']['code'], -32015)
        self.assertEqual(reply['error']['data']['reason'], 'challenge_failed')
        self.assertEqual(self.app.mcp.status()['subscriptions'], 0)

    def test_filters_expiry_unsubscribe_and_revoked_key_stop_delivery(self):
        sub, params = self.subscribe(self.context)
        other = self.app.store.create_context('Other fixture')['id']
        request = self.request(context=other)
        self.app.mcp.queue_pending()
        self.assertFalse(self.app.mcp.process_one())
        request = self.request()
        self.app.mcp.queue_pending()
        self.app.auth.revoke(self.key_row['id'])
        self.app.mcp.process_one()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.app.mcp.status()['subscriptions'], 0)
        with self.assertRaises(PermissionError): self.tool('get_request', request)

    def test_expired_subscription_and_unsubscribe_are_idempotent(self):
        sub, params = self.subscribe()
        self.app.mcp.unsubscribe(self.key_row['id'], params)
        self.app.mcp.unsubscribe(self.key_row['id'], params)
        self.request(); self.app.mcp.queue_pending()
        self.assertFalse(self.app.mcp.process_one())
        self.subscribe()
        with self.app.store.connect() as db: db.execute('UPDATE mcp_subscriptions SET expires=0')
        self.app.mcp.queue_pending()
        self.assertFalse(self.app.mcp.process_one())

    def test_reactivating_old_subscription_respects_active_limit(self):
        _, params = self.subscribe()
        self.app.mcp.unsubscribe(self.key_row['id'], params)
        other = self.app.store.create_context('Other fixture')['id']
        self.subscribe(context=other)
        with patch('hub_mcp_events.MAX_SUBSCRIPTIONS', 1):
            with self.assertRaises(ValueError): self.app.mcp.subscribe(self.key_row['id'], params)

    def test_retries_keep_event_id_body_and_durable_queue_across_restart(self):
        self.subscribe(); self.request()
        self.app.mcp.queue_pending()
        events = []
        def transient(url, body, headers):
            events.append((body, headers)); return 503, b'{}'
        self.app.mcp.sender = transient
        self.assertTrue(self.app.mcp.process_one())
        self.app = HubApplication(self.config)
        self.app.mcp.sender = transient
        with self.app.store.connect() as db: db.execute('UPDATE mcp_deliveries SET next_attempt=0')
        self.assertTrue(self.app.mcp.process_one())
        self.assertEqual(events[0][0], events[1][0])
        self.assertEqual(events[0][1]['webhook-id'], events[1][1]['webhook-id'])
        self.assertEqual(self.app.mcp.status()['pending'], 1)

    def test_410_and_413_are_not_retried(self):
        for status in (410, 413):
            self.subscribe(); self.request(); self.app.mcp.queue_pending()
            self.app.mcp.sender = lambda *_: (status, b'{}')
            self.assertTrue(self.app.mcp.process_one())
            with self.app.store.connect() as db:
                row = db.execute('SELECT status FROM mcp_deliveries ORDER BY rowid DESC LIMIT 1').fetchone()
                self.assertEqual(row['status'], 'dead')
            self.app.mcp.sender = self.send

    def test_indexed_context_reply_deduplication_and_shared_history(self):
        self.app.store.put_memory('fixture_weather', 'Тестовый город: погода ветреная', 'fixture', 0)
        self.app.store.put_memory('fixture_recipe', 'Рецепт синтетического печенья', 'fixture', 0)
        row = self.request()
        packet = self.tool('get_request', row)
        bodies = encoded(packet['context']['memory'])
        self.assertIn('ветреная', bodies)
        self.assertNotIn('печенья', bodies)
        self.assertLess(len(encoded(packet['context']).encode()), 25000)
        first = self.tool('submit_result', row, reply='По сохранённым данным в тестовом городе ветрено.', error='')
        second = self.tool('submit_result', row, reply='По сохранённым данным в тестовом городе ветрено.', error='')
        self.assertEqual(first, second)
        with self.assertRaises(Conflict): self.tool('submit_result', row, reply='Другое.', error='')
        messages = [e for e in self.app.store.history(self.context) if e['role']=='assistant']
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.app.mcp.status()['pending'], 0)
        self.assertEqual(self.app.store.task(row['id'])['status'], 'completed')

    def test_restart_never_replays_an_unpersisted_external_dispatch(self):
        row, _ = self.app.store.create_task(self.context, 'fixture-client', 'dot', 'Synthetic interrupted dispatch', uid())
        self.app = HubApplication(self.config)
        self.assertEqual(self.app.store.task(row['id'])['status'], 'unknown')
        self.assertEqual(self.app.mcp.status()['pending'], 0)

    def test_reply_is_persisted_in_synthetic_phone_inbox_once(self):
        device = uid()
        self.app.mobile.sync(device, {'version':1,'revision':1,'people':[],'sources':[],'note':None})
        self.app.client_focus('android:'+device)
        row = self.request()
        self.tool('submit_result', row, reply='Синтетический ответ для inbox.', error='')
        self.tool('submit_result', row, reply='Синтетический ответ для inbox.', error='')
        events = self.app.mobile.link.events(device)['events']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['body'], 'Синтетический ответ для inbox.')

    def test_phone_inbox_failure_can_recover_after_committed_reply(self):
        device = uid()
        self.app.mobile.sync(device, {'version':1,'revision':1,'people':[],'sources':[],'note':None})
        self.app.client_focus('android:'+device)
        row = self.request()
        with patch.object(self.app.mobile, 'deliver', side_effect=OSError('Synthetic inbox failure')):
            with self.assertRaises(OSError): self.tool('submit_result', row, reply='Сохранённый ответ.', error='')
        self.app = HubApplication(self.config)
        self.app.mcp.maintain()
        self.assertEqual(self.app.store.task(row['id'])['status'], 'completed')
        self.assertEqual(len([e for e in self.app.store.history(self.context) if e['role']=='assistant']), 1)
        self.assertEqual(self.app.mobile.link.events(device)['events'][0]['body'], 'Сохранённый ответ.')

    def test_cancelled_or_expired_request_cannot_act_or_submit(self):
        row = self.request('Поставь таймер на пять минут')
        self.app.coordinator.cancel(row['id'])
        with self.assertRaises(Conflict): self.tool('submit_result', row, reply='Готово', error='')
        with self.assertRaises(Conflict): self.tool('capabilities_list', row, query='timer')
        row = self.request()
        with self.app.store.connect() as db: db.execute('UPDATE dot_requests SET expires=0 WHERE id=?', (row['id'],))
        self.app.mcp.maintain()
        task = self.app.store.task(row['id'])
        self.assertEqual(task['status'], 'failed')
        self.assertIn('Dot не ответил вовремя', task['error'])
        with self.assertRaises(Conflict): self.tool('submit_result', row, reply='Поздний ответ', error='')

    def test_timeout_and_cancellation_deliver_terminal_status_once(self):
        device = uid()
        self.app.mobile.sync(device, {'version':1,'revision':1,'people':[],'sources':[],'note':None})
        self.app.client_focus('android:'+device)
        cancelled = self.request()
        self.app.coordinator.cancel(cancelled['id'])
        expired = self.request()
        with self.app.store.connect() as db:
            db.execute('UPDATE dot_requests SET expires=0 WHERE id=?', (expired['id'],))
        self.app.mcp.maintain(); self.app.mcp.maintain()
        events = self.app.mobile.link.events(device)['events']
        self.assertEqual(len(events), 2)
        self.assertTrue(any('остановлен' in e['body'] for e in events))
        self.assertTrue(any('не ответил вовремя' in e['body'] for e in events))

    def test_real_hub_tool_idempotency_and_device_acknowledgement(self):
        row = self.request('Поставь таймер на пять минут')
        catalog = self.tool('capabilities_list', row, query='timer')
        self.assertEqual(catalog[0]['id'], 'hub')
        args = {'node':'hub','capability':'timer.create','arguments_json':'{"duration_seconds":300}', 'request_key':'timer-1'}
        first = self.tool('capability_call', row, **args)
        second = self.tool('capability_call', row, **args)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.tool('capability_result', row, call_id=first['id'])['status'], 'succeeded')
        self.assertEqual(len(self.app.fabric.timers(self.context)), 1)
        capabilities = [{'name':'fixture.action','description':'Synthetic remote action',
                         'inputSchema':{'type':'object','properties':{},'required':[],'additionalProperties':False}}]
        self.app.fabric.register('fixture-node', 'Synthetic remote node', 'fixture', capabilities)
        remote = self.tool('capability_call', row, node='fixture-node', capability='fixture.action', arguments_json='{}', request_key='remote-1')
        self.assertEqual(remote['status'], 'queued')
        claimed = self.app.fabric.claim('fixture-node')
        self.app.fabric.complete('fixture-node', claimed['id'], claimed['lease'], {'done':True})
        self.assertEqual(self.tool('capability_result', row, call_id=remote['id'])['status'], 'succeeded')
        with self.assertRaises(ValueError): self.tool('agent_task', row, agent='dot', prompt='x')

    def test_callback_destinations_block_private_mixed_and_redirect_tricks(self):
        for url in ('http://example.com', 'https://user@example.com', 'https://example.com:8443', 'https://example.com/#x', 'https://example.com\\@localhost'):
            with self.assertRaises(ValueError): callback_url(url)
        with patch('hub_mcp_events.socket.getaddrinfo', return_value=[(0,0,0,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError): public_addresses('callback.example.com')
        with patch('hub_mcp_events.socket.getaddrinfo', return_value=[(0,0,0,'',('8.8.8.8',443)),(0,0,0,'',('10.0.0.1',443))]):
            with self.assertRaises(ValueError): public_addresses('callback.example.com')

    def test_callback_connection_pins_validated_ip_and_keeps_tls_hostname(self):
        sock, context = Mock(), Mock()
        with patch('hub_mcp_events.ssl.create_default_context', return_value=context), patch('hub_mcp_events.socket.create_connection', return_value=sock) as connect:
            client = PinnedHTTPS('callback.example.com', '8.8.8.8', 10)
            client.connect()
            connect.assert_called_once_with(('8.8.8.8',443), 10)
            context.wrap_socket.assert_called_once_with(sock, server_hostname='callback.example.com')
        with patch('hub_mcp_events.public_addresses', return_value=['8.8.8.8']), patch('hub_mcp_events.PinnedHTTPS') as factory:
            response = factory.return_value.getresponse.return_value
            response.status, response.read.return_value = 302, b''
            self.assertEqual(webhook_post('https://callback.example.com/events', b'{}', {}), (302,b''))
            factory.return_value.request.assert_called_once_with('POST','/events',b'{}',{})
            factory.return_value.close.assert_called_once_with()


class HttpOAuthTests(DotFixture):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(('127.0.0.1',0), HubHandler)
        self.server.app = self.app
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        super().tearDown()

    def http(self, method, path, body=None, token=None, headers=None, form=False):
        h = {'Host':'hub.example.com', 'X-Forwarded-Proto':'https', 'X-Forwarded-For':'203.0.113.5',
             'Content-Type':'application/x-www-form-urlencoded' if form else 'application/json'}
        if token is not None: h['Authorization'] = 'Bearer '+token
        h.update(headers or {})
        client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        data = urllib.parse.urlencode(body) if form else json.dumps(body) if body is not None else None
        client.request(method,path,data,h)
        response = client.getresponse(); result = (response.status,dict(response.getheaders()),response.read())
        client.close(); return result

    def test_private_stdio_relay_forwards_events_with_scoped_key(self):
        token = Path(self.tmp.name)/'dot.key'
        token.write_text(self.key); token.chmod(0o600)
        source = io.BytesIO((encoded({'jsonrpc':'2.0','id':1,'method':'server/discover'})+'\n'+
                             encoded({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n'+
                             encoded({'jsonrpc':'2.0','id':2,'method':'events/list'})+'\n').encode())
        output = io.BytesIO()
        with patch.dict('os.environ', {'http_proxy':'http://127.0.0.1:1'}):
            relay({'host':'127.0.0.1','port':self.server.server_port}, token, source, output)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]['result']['supportedVersions'], [VERSION])
        self.assertEqual(lines[1]['result']['events'][0]['name'], EVENT)
        self.assertNotIn(self.key.encode(), output.getvalue())

    def oauth(self):
        status, _, body = self.http('POST','/oauth/register', {'client_name':'Fixture dot','redirect_uris':[REDIRECT], 'token_endpoint_auth_method':'none'})
        self.assertEqual(status, 201)
        client = json.loads(body)['client_id']
        verifier = 'v'*48
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        params = {'client_id':client,'response_type':'code','redirect_uri':REDIRECT,'state':'fixture-state',
                  'code_challenge':challenge,'code_challenge_method':'S256','resource':'https://hub.example.com/mcp','scope':'hub:requests'}
        status, headers, page = self.http('GET','/oauth/authorize?'+urllib.parse.urlencode(params), headers={'Sec-Fetch-Site':'cross-site'})
        self.assertEqual(status, 200)
        self.assertNotIn(self.owner.encode(), page)
        cookie = headers['Set-Cookie'].split(';')[0]
        nonce = cookie.split('=',1)[1]
        status, headers, _ = self.http('POST','/oauth/authorize', {'nonce':nonce,'token':self.owner},
                                     headers={'Origin':'https://hub.example.com','Cookie':cookie}, form=True)
        self.assertEqual(status, 303)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(headers['Location']).query)
        self.assertEqual(query['iss'], ['https://hub.example.com'])
        self.assertEqual(query['state'], ['fixture-state'])
        exchange = {'client_id':client,'grant_type':'authorization_code','code':query['code'][0],
                    'redirect_uri':REDIRECT,'resource':params['resource'],'code_verifier':verifier}
        status, _, body = self.http('POST','/oauth/token', exchange, form=True)
        self.assertEqual(status, 200)
        return json.loads(body), exchange

    def test_complete_oauth_then_mcp_scopes_and_revocation(self):
        status, headers, _ = self.http('POST','/mcp', {'jsonrpc':'2.0','id':1,'method':'server/discover'})
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata', headers['WWW-Authenticate'])
        status, _, body = self.http('GET','/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(json.loads(body)['resource'], 'https://hub.example.com/mcp')
        tokens, exchange = self.oauth()
        principal = self.app.oauth.resolve(tokens['access_token'])
        self.assertIsNotNone(principal)
        self.assertEqual(self.http('POST','/mcp', {'jsonrpc':'2.0','id':1,'method':'tools/list'}, tokens['access_token'])[0], 200)
        for path in ('/api/state','/api/memory','/api/nodes','/link/fake/knowledge'):
            self.assertEqual(self.http('GET',path, token=tokens['access_token'])[0], 403)
        self.assertEqual(self.http('POST','/oauth/token', exchange, form=True)[0], 400)
        self.app.auth.revoke(principal)
        self.assertEqual(self.http('POST','/mcp', {'jsonrpc':'2.0','id':1,'method':'events/list'}, tokens['access_token'])[0], 401)

    def test_pkce_audience_rotation_and_secret_digest_storage(self):
        tokens, exchange = self.oauth()
        refresh = {'grant_type':'refresh_token','refresh_token':tokens['refresh_token'], 'client_id':exchange['client_id'], 'resource':exchange['resource']}
        self.assertEqual(self.http('POST','/oauth/token', {**refresh,'resource':'https://other.example.com/mcp'}, form=True)[0], 400)
        status, _, body = self.http('POST','/oauth/token', refresh, form=True)
        self.assertEqual(status, 200)
        new = json.loads(body)
        self.assertNotEqual(new['refresh_token'], tokens['refresh_token'])
        self.assertEqual(self.http('POST','/oauth/token', refresh, form=True)[0], 400)
        with self.app.store.connect() as db:
            saved = encoded([dict(r) for r in db.execute('SELECT * FROM mcp_oauth_tokens')])
        self.assertNotIn(tokens['access_token'], saved)
        self.assertNotIn(new['refresh_token'], saved)
        with patch('hub_mcp_oauth.time.time', return_value=time.time()+3601):
            self.assertIsNone(self.app.oauth.resolve(new['access_token']))

    def test_invalid_pkce_redirect_nonce_and_registration_never_grant(self):
        oauth = self.app.oauth
        with self.assertRaises(OAuthError): oauth.register({'redirect_uris':['https://evil.example.com/callback']})
        client = oauth.register({'redirect_uris':[REDIRECT]})['client_id']
        params = {'client_id':client,'response_type':'code','redirect_uri':REDIRECT,'state':'s','code_challenge':'p'*43,
                  'code_challenge_method':'plain','resource':'https://hub.example.com/mcp'}
        with self.assertRaises(OAuthError): oauth.consent(params)
        params['code_challenge_method'] = 'S256'
        _, nonce = oauth.consent(params)
        with self.assertRaises(OAuthError): oauth.approve(nonce, 'wrong', self.owner)
        with self.assertRaises(PermissionError): oauth.approve(nonce, nonce, self.key)
        location = oauth.approve(nonce, nonce, self.owner)
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)['code'][0]
        exchange = {'grant_type':'authorization_code','client_id':client,'code':code,'redirect_uri':REDIRECT,
                    'resource':'https://hub.example.com/mcp','code_verifier':'x'*48}
        with self.assertRaises(OAuthError): oauth.exchange(exchange)
        with self.assertRaises(OAuthError): oauth.approve(nonce, nonce, self.owner)

    def test_mcp_and_webhook_keys_are_separate_and_notifications_get_202(self):
        rpc = {'jsonrpc':'2.0','id':1,'method':'server/discover'}
        self.assertEqual(self.http('POST','/mcp',rpc,self.key)[0], 200)
        self.assertEqual(self.http('POST','/mcp',rpc,self.web_key)[0], 403)
        self.assertEqual(self.http('POST','/webhooks/events',{},self.key)[0], 403)
        self.assertEqual(self.http('POST','/mcp', {'jsonrpc':'2.0','method':'notifications/initialized'},self.key)[0], 202)
        self.assertEqual(self.http('GET','/mcp',token=self.key)[0], 405)


if __name__ == '__main__':
    unittest.main()
