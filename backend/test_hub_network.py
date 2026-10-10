"""Listener selection without opening any LAN/VPN socket."""
import unittest
from types import SimpleNamespace
from hub_network import bind_ip, hub_url, ProxyOrigin
from hub_server import HubHandler


class NetworkTests(unittest.TestCase):
    def test_only_literal_loopback_and_private_addresses_are_supported(self):
        for host in ('127.0.0.1','::1','10.42.0.1','172.20.0.1','192.168.50.1','100.80.0.1','fd00::1'):
            self.assertEqual(str(bind_ip(host)),host)
        for host in ('0.0.0.0','::','8.8.8.8','169.254.1.1','100.128.0.1','localhost','example.invalid'):
            with self.assertRaises(ValueError):bind_ip(host)
        self.assertEqual(hub_url({'host':'fd00::1','port':8778}),'http://[fd00::1]:8778')

    def test_origin_uses_explicit_listener_not_a_random_lan_host(self):
        handler=object.__new__(HubHandler)
        handler.server=SimpleNamespace(server_address=('10.42.0.1',8778),server_port=8778)
        handler.headers={'Host':'10.42.0.1:8778','Origin':'http://10.42.0.1:8778'}
        handler.check_origin()
        for headers in ({'Host':'localhost:8778'},{'Host':'10.42.0.1:8778','Origin':'http://10.42.0.2:8778'}):
            handler.headers=headers
            with self.assertRaises(PermissionError):handler.check_origin()

    def test_public_origin_is_explicit_https_and_proxies_are_private(self):
        for url in ('https://hub.example.com','https://hub.example.com:8443/',
                    'https://[2001:db8::1]:443','https://203.0.113.1'):
            config=ProxyOrigin({'public_url':url,'trusted_proxies':['127.0.0.1','::1','10.42.0.0/24']})
            self.assertEqual(config.origin,url.rstrip('/'))
            self.assertTrue(config.trusted('127.0.0.1'))
            self.assertTrue(config.trusted('10.42.0.3'))
            self.assertFalse(config.trusted('203.0.113.1'))
        for url in ('http://hub.example.com','https://u:p@hub.example.com','https://hub.example.com/path',
                    'https://hub.example.com?token=x','https://hub.example.com#x','https://hub.example.com:',
                    'https://hub.example.com:0','https://Hub.example.com','https://hub.example.com.',
                    'https://hub%2eexample.com','https://hub.example.com:99999','https://hub.example.com\\evil'):
            with self.assertRaises(ValueError):ProxyOrigin({'public_url':url,'trusted_proxies':['127.0.0.1']})
        for proxies in ([], ['0.0.0.0/0'], ['::/0'], ['8.8.8.8'], ['10.42.0.2/24'], '127.0.0.1'):
            with self.assertRaises(ValueError):ProxyOrigin({'public_url':'https://hub.example.com','trusted_proxies':proxies})
        with self.assertRaises(ValueError):ProxyOrigin({'trusted_proxies':['127.0.0.1']})
        self.assertIsNone(ProxyOrigin({}).origin)


if __name__=='__main__':unittest.main()
