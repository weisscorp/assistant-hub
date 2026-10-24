"""Synthetic GitHub responses only: never queries a real package or token."""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from check_ghcr_visibility import check_private_package


class GhcrVisibilityTests(unittest.TestCase):
    def response(self, package):
        return io.BytesIO(json.dumps(package).encode())

    @patch('check_ghcr_visibility.urllib.request.urlopen')
    def test_personal_namespace_existing_private_package(self, open_url):
        open_url.return_value = self.response({'visibility': 'private'})
        self.assertIn('private', check_private_package('weisscorp', 'assistant-hub', 'synthetic'))
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url,
            'https://api.github.com/users/weisscorp/packages/container/assistant-hub')
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic')

    @patch('check_ghcr_visibility.urllib.request.urlopen')
    def test_existing_public_internal_or_missing_visibility_blocks_push(self, open_url):
        for visibility in ('public', 'internal', None):
            with self.subTest(visibility=visibility):
                open_url.return_value = self.response({'visibility': visibility})
                with self.assertRaisesRegex(RuntimeError, 'not private'):
                    check_private_package('weisscorp', 'assistant-hub', 'synthetic')

    @patch('check_ghcr_visibility.urllib.request.urlopen')
    def test_new_package_can_use_private_default(self, open_url):
        open_url.side_effect = urllib.error.HTTPError('synthetic', 404, 'not found', {}, None)
        self.assertIn('New GHCR package', check_private_package('weisscorp', 'assistant-hub', 'synthetic'))

    @patch('check_ghcr_visibility.urllib.request.urlopen')
    def test_auth_rate_limit_and_server_failures_block_push(self, open_url):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                open_url.side_effect = urllib.error.HTTPError('synthetic', status, 'failure', {}, None)
                with self.assertRaisesRegex(RuntimeError, str(status)):
                    check_private_package('weisscorp', 'assistant-hub', 'synthetic')

    @patch('check_ghcr_visibility.urllib.request.urlopen')
    def test_invalid_response_and_network_error_block_push(self, open_url):
        open_url.return_value = io.BytesIO(b'not json')
        with self.assertRaises(ValueError):
            check_private_package('weisscorp', 'assistant-hub', 'synthetic')
        open_url.side_effect = urllib.error.URLError('synthetic network failure')
        with self.assertRaises(OSError):
            check_private_package('weisscorp', 'assistant-hub', 'synthetic')


if __name__ == '__main__':
    unittest.main()
