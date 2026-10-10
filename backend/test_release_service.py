import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from release_service import private_file, release


class ReleaseServiceTest(unittest.TestCase):
    def test_private_config_permissions_and_unprivileged_entry_refusal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'config.json'; config.write_text('{}'); config.chmod(0o600)
            self.assertEqual(config.resolve(), private_file(config))
            config.chmod(0o644)
            with self.assertRaises(ValueError): private_file(config)
            config.chmod(0o600); root.chmod(0o755)
            with self.assertRaises(ValueError): private_file(config)
            with patch('release_service.os.geteuid', return_value=12001), patch('release_service.build_release') as build:
                with self.assertRaises(ValueError): release(config, 8)
                build.assert_not_called()

    def test_fixed_release_config_clean_environment_and_separate_build_user(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ):
            root = Path(temporary).resolve()
            checkout = root / 'checkout'; checkout.mkdir()
            key = root / 'key.jks'; key.write_bytes(b'fixture')
            credentials = root / 'credentials.json'; credentials.write_text(json.dumps({'KEYSTORE_PASSWORD': 'fixture-pass', 'KEY_PASSWORD': 'fixture-pass'}))
            config = root / 'config.json'; config.write_text(json.dumps({'build_user': 'helmet-build', 'checkout': str(checkout),
                'credentials_file': str(credentials), 'keystore': str(key), 'build_tools': str(root), 'java_home': str(root),
                'android_home': str(root), 'output': str(root / 'releases'), 'log_dir': str(root / 'logs'),
                'alias': 'helmet', 'certificate_sha256': 'a' * 64, 'minimum_version': 7}))
            identity = SimpleNamespace(pw_uid=checkout.stat().st_uid)
            os.environ['JAVA_TOOL_OPTIONS'] = '-javaagent:untrusted.jar'
            os.environ['PYTHONPATH'] = '/untrusted'
            os.environ['HELMET_MODEL_API_KEY'] = 'fixture-model-key'
            original = dict(os.environ)
            def build(*args):
                self.assertNotIn('JAVA_TOOL_OPTIONS', os.environ)
                self.assertNotIn('PYTHONPATH', os.environ)
                self.assertNotIn('HELMET_MODEL_API_KEY', os.environ)
                self.assertEqual('fixture-pass', os.environ['KEYSTORE_PASSWORD'])
                self.assertEqual((8, 'helmet-build'), args[-2:])
                return {'versionCode': 8}
            with patch('release_service.os.geteuid', return_value=0), patch('release_service.pwd.getpwnam', return_value=identity), \
                    patch('release_service.private_file', side_effect=lambda path: Path(path)), \
                    patch('release_service.private_keystore', return_value=key), \
                    patch('release_service.trusted_path', side_effect=lambda path: Path(path)), \
                    patch('release_service.build_release', side_effect=build):
                self.assertEqual({'versionCode': 8}, release(config, 8))
                with self.assertRaises(ValueError): release(config, 1000000)
            self.assertEqual(original, dict(os.environ))


if __name__ == '__main__': unittest.main()
