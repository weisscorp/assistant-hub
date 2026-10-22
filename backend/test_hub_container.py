"""Container config/secrets and explicit Host aliases; no owner runtime is used."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from hub_container import configuration
from hub_network import http_origins
from hub_server import HubHandler, private_json


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.token = self.root / 'mounted-token'
        self.token.write_text('synthetic-owner-token-' + 'x' * 32)
        self.env = {'HUB_DATA_DIR': str(self.root / 'data'), 'HUB_RUNTIME_DIR': str(self.root / 'runtime'),
            'HUB_OWNER_TOKEN_FILE': str(self.token), 'HUB_HOST': '172.20.0.2',
            'HUB_HTTP_ORIGINS': 'http://hub:8778,http://truenas.example:8778'}

    def test_mounted_config_and_secret_become_private_owned_runtime_files(self):
        mounted = self.root / 'config-map.json'
        mounted.write_text(json.dumps({'providers': {}, 'state': '/wrong', 'token_file': '/wrong'}))
        mounted.chmod(0o644)
        self.env['HUB_CONFIG_FILE'] = str(mounted)
        config = private_json(configuration(self.env))
        self.assertEqual(config['providers'], {})
        self.assertEqual(config['state'], str(self.root / 'data' / 'state'))
        copied = Path(config['token_file'])
        self.assertEqual(copied.read_text(), self.token.read_text())
        self.assertEqual(copied.stat().st_mode & 0o777, 0o600)

    def test_default_provider_and_restart_reuse_explicit_secret(self):
        first = private_json(configuration(self.env))
        second = private_json(configuration(self.env))
        self.assertEqual(first['token_file'], second['token_file'])
        self.assertEqual(first['providers']['ollama']['type'], 'ollama')
        self.env['HUB_HOST'] = '0.0.0.0'
        with self.assertRaises(ValueError):
            configuration(self.env)

    def test_missing_secret_and_public_runtime_directory_fail_closed(self):
        self.env.pop('HUB_OWNER_TOKEN_FILE')
        with self.assertRaises(ValueError):
            configuration(self.env)
        self.env['HUB_OWNER_TOKEN_FILE'] = str(self.token)
        (self.root / 'runtime').chmod(0o755)
        with self.assertRaises(ValueError):
            configuration(self.env)

    def test_http_aliases_are_explicit_and_origin_and_forwarded_checks_remain(self):
        config = private_json(configuration(self.env))
        handler = object.__new__(HubHandler)
        from hub_network import ProxyOrigin
        handler.server = SimpleNamespace(server_address=('172.20.0.2', 8778), server_port=8778,
            app=SimpleNamespace(config=config, proxy=ProxyOrigin(config)))
        handler.headers = {'Host': 'hub:8778', 'Origin': 'http://hub:8778'}
        handler.check_origin()
        for headers in ({'Host': 'unlisted:8778'}, {'Host': 'hub:8778', 'Origin': 'http://evil.invalid'},
                        {'Host': 'hub:8778', 'X-Forwarded-Proto': 'https'}):
            handler.headers = headers
            with self.assertRaises(PermissionError):
                handler.check_origin()

    def test_malformed_http_aliases_are_rejected(self):
        for value in ('http://u:p@hub', 'http://hub/path', 'http://hub?token=x', 'http://0.0.0.0:8778',
                      'http://8.8.8.8:8778', 'http://Hub:8778', 'http://hub:', 'https://hub:8778'):
            with self.assertRaises(ValueError, msg=value):
                http_origins({'http_origins': [value]})


if __name__ == '__main__':
    unittest.main()
