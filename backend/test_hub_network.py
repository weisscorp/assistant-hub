"""Listener selection without opening any LAN/VPN socket."""
import unittest
from types import SimpleNamespace
from hub_network import bind_ip, hub_url
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


if __name__=='__main__':unittest.main()
