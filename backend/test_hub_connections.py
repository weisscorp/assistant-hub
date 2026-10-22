import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from agent_hub import Conflict
from hub_connections import HermesConnections, vpn_endpoint, check_dns
import test_hub_auth as auth_fixture


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = HermesConnections(self.temp.name)
        self.probe = patch.object(self.manager, 'probe', return_value={'ready':True,'checked_at':1,'detail':'synthetic'})
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        self.temp.cleanup()

    def body(self, **changes):
        return {'revision':self.manager.snapshot()['revision'],'name':'Mini','url':'https://mini.example.ts.net:8444/v1',
                'model':'hermes-agent','token':'synthetic-connection-key-123',**changes}

    def save(self, **changes):
        return self.manager.save(self.body(**changes))['connection']['id']

    def test_save_does_not_activate_or_return_secret(self):
        result=self.manager.save(self.body())
        self.assertIsNone(result['settings']['active'])
        self.assertNotIn('synthetic-connection-key-123',json.dumps(result))
        self.assertNotIn('key_id',json.dumps(result))
        for path in self.manager.directory.iterdir():self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_test_only_does_not_save(self):
        self.manager.save(self.body(),save=False)
        self.assertEqual(self.manager.snapshot()['profiles'],[])
        self.assertEqual(len(list(self.manager.directory.glob('*.key'))),0)

    def test_switch_survives_restart(self):
        pid=self.save()
        self.manager.activate({'id':pid,'revision':1})
        again=HermesConnections(self.temp.name)
        self.assertEqual(again.snapshot()['active'],pid)
        self.assertEqual(again.status()['detail'],'Mini · hermes-agent')

    def test_failed_check_keeps_active_server_and_revision(self):
        first=self.save()
        self.manager.activate({'id':first,'revision':1})
        second=self.save(name='Other',url='https://other.example.ts.net/v1')
        before=self.manager.snapshot()
        self.manager.probe.side_effect=ValueError('offline')
        with self.assertRaises(ValueError):self.manager.activate({'id':second,'revision':before['revision']})
        self.assertEqual(self.manager.snapshot(),before)

    def test_stale_revision_and_new_destination_cannot_reuse_key(self):
        pid=self.save()
        with self.assertRaises(Conflict):self.manager.save(self.body(revision=0))
        with self.assertRaisesRegex(ValueError,'отдельный ключ'):
            self.manager.save(self.body(id=pid,url='https://other.example.ts.net/v1',token=''))
        self.manager.save(self.body(id=pid,name='Renamed',token=''))
        self.assertEqual(self.manager._secret(self.manager._profile(pid)),'synthetic-connection-key-123')

    def test_failed_disk_write_preserves_previous_credential(self):
        pid=self.save()
        before=self.manager.snapshot()
        with patch.object(self.manager,'_persist',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.manager.save(self.body(id=pid,token='different-synthetic-key'))
        self.assertEqual(self.manager.snapshot(),before)
        again=HermesConnections(self.temp.name)
        self.assertEqual(again._secret(again._profile(pid)),'synthetic-connection-key-123')

    def test_private_files_cannot_be_symlinks(self):
        self.manager.path.unlink()
        target=Path(self.temp.name)/'public.json';target.write_text('{}')
        self.manager.path.symlink_to(target)
        with self.assertRaises(ValueError):HermesConnections(self.temp.name)

    def test_vpn_only_no_url_credentials_or_redirect_destination(self):
        for url in ['https://example.com/v1','http://169.254.169.254/v1','http://192.168.1.1/v1',
                    'https://user:key@mini.ts.net/v1','https://mini.ts.net/v1?key=secret',
                    'https://mini.ts.net/v1#key','file:///v1','http://mini.ts.net/v1']:
            with self.subTest(url=url),self.assertRaises(ValueError):vpn_endpoint(url)
        self.assertEqual(vpn_endpoint('https://mini.ts.net:8444/v1/'),'https://mini.ts.net:8444/v1')
        with patch('hub_connections.socket.getaddrinfo',return_value=[(0,0,0,'',('8.8.8.8',443))]):
            with self.assertRaises(ValueError):check_dns('https://mini.ts.net/v1')

    def test_current_turn_uses_credential_snapshot(self):
        pid=self.save()
        self.manager.activate({'id':pid,'revision':1})
        entered,release=threading.Event(),threading.Event()
        captured=[]
        def turn(config,body,*,secret):
            entered.set();release.wait(2);captured.append((config['url'],secret));return 'ok'
        class Coordinator:
            def context_packet(self,_):return {}
        with patch('hub_connections.check_dns'),patch('hub_connections.turn',side_effect=turn):
            thread=threading.Thread(target=self.manager.run,args=({},Coordinator(),threading.Event(),lambda _:None))
            thread.start();self.assertTrue(entered.wait(1))
            self.manager.save(self.body(id=pid,token='new-synthetic-key-123'))
            release.set();thread.join(2)
        self.assertEqual(captured,[('https://mini.example.ts.net:8444/v1','synthetic-connection-key-123')])


class ConnectionApiTests(unittest.TestCase):
    setUp=auth_fixture.PublicApiTests.setUp
    tearDown=auth_fixture.PublicApiTests.tearDown
    request=auth_fixture.PublicApiTests.request
    setup_account=auth_fixture.PasswordApiTests.setup_account
    origin=auth_fixture.PasswordApiTests.origin

    def test_only_password_owner_can_configure_not_any_application_key(self):
        cookie=self.setup_account()
        for scope in ['owner','app','webhook','mcp']:
            _,key=self.app.auth.issue('Synthetic '+scope,scope)
            self.assertEqual(self.request('/api/connections',key)[0],403)
            self.assertEqual(self.request('/api/connections/save',key,{'revision':0})[0],403)
        self.assertEqual(self.request('/api/connections')[0],401)
        self.assertEqual(self.request('/api/connections',headers=cookie)[0],200)

    def test_owner_save_test_switch_and_csrf(self):
        cookie=self.setup_account()
        body={'revision':0,'name':'Mini','url':'https://mini.ts.net:8444/v1','model':'hermes-agent','token':'synthetic-private-key-123'}
        with patch.object(self.app.hermes_connections,'probe',return_value={'ready':True,'detail':'synthetic','checked_at':1}):
            self.assertEqual(self.request('/api/connections/test',body=body,headers=cookie)[0],200)
            response=self.request('/api/connections/save',body=body,headers=cookie)
            self.assertEqual(response[0],200)
            self.assertNotIn(body['token'].encode(),response[2])
            pid=json.loads(response[2])['connection']['id']
            self.assertEqual(self.request('/api/connections/activate',body={'id':pid,'revision':1},headers={**cookie,'Origin':'https://evil.example'})[0],401)
            result=self.request('/api/connections/activate',body={'id':pid,'revision':1},headers=cookie)
            self.assertEqual(result[0],200)
            self.assertEqual(json.loads(result[2])['active'],pid)


if __name__=='__main__':unittest.main()
