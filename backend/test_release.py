"""Publication invariants; Android tool integration is verified separately on the built APK."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from make_release import stage, inspect_apk

CERTIFICATE = 'a' * 64


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.apk = self.root / 'input.apk'
        self.apk.write_bytes(b'first APK')
        self.output = self.root / 'releases'
        self.tools = self.root / 'tools'

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, version, assertion=None):
        with patch('make_release.inspect_apk', return_value=version):
            return stage(self.apk, self.output, self.tools, CERTIFICATE, assertion)

    def test_atomic_manifest_points_to_immutable_verified_snapshot(self):
        first = self.publish(6, 6)
        artifact = self.output / first['file']
        self.assertEqual(hashlib.sha256(b'first APK').hexdigest(), first['sha256'])
        self.assertEqual(b'first APK', artifact.read_bytes())
        self.assertEqual(0o444, artifact.stat().st_mode & 0o777)
        self.apk.write_bytes(b'second APK')
        second = self.publish(7)
        self.assertEqual(second, json.loads((self.output / 'manifest.json').read_text()))
        self.assertEqual(b'first APK', artifact.read_bytes())
        self.assertFalse(any(self.output.glob('.release-*')))

    def test_retry_is_idempotent_and_downgrade_or_same_version_changes_are_rejected(self):
        first = self.publish(6)
        self.assertEqual(first, self.publish(6))
        for version, data in [(5, b'first APK'), (6, b'different APK')]:
            self.apk.write_bytes(data)
            with self.assertRaises(ValueError): self.publish(version)
        self.assertEqual(first, json.loads((self.output / 'manifest.json').read_text()))

    def test_wrong_assertion_or_failed_verification_does_not_publish(self):
        with self.assertRaises(ValueError): self.publish(6, 7)
        with patch('make_release.inspect_apk', side_effect=subprocess.CalledProcessError(1, ['apksigner'])):
            with self.assertRaises(subprocess.SubprocessError):
                stage(self.apk, self.output, self.tools, CERTIFICATE)
        self.assertFalse((self.output / 'manifest.json').exists())
        self.assertFalse(list(self.output.glob('*.apk')))

    def test_corrupted_previous_release_cannot_be_silently_replaced(self):
        first = self.publish(6)
        artifact = self.output / first['file']
        artifact.chmod(0o644); artifact.write_bytes(b'corrupt')
        self.apk.write_bytes(b'next APK')
        with self.assertRaises(ValueError): self.publish(7)
        self.assertEqual(first, json.loads((self.output / 'manifest.json').read_text()))

    def test_download_size_boundary_and_rejected_candidate_preserve_previous_release(self):
        with patch('make_release.MAX_APK', 9):
            first = self.publish(6)  # first APK is exactly nine bytes
            self.apk.write_bytes(b'0123456789')
            with patch('make_release.inspect_apk') as inspect:
                with self.assertRaisesRegex(ValueError, 'phone download limit'):
                    stage(self.apk, self.output, self.tools, CERTIFICATE)
                inspect.assert_not_called()
        self.assertEqual(first, json.loads((self.output / 'manifest.json').read_text()))
        self.assertEqual(b'first APK', (self.output / first['file']).read_bytes())
        self.assertEqual(1, len(list(self.output.glob('*.apk'))))
        self.assertFalse(any(self.output.glob('.release-*')))

    def test_inspection_checks_signature_alignment_identity_and_version(self):
        signature = f'Signer #1 certificate SHA-256 digest: {CERTIFICATE}\n'
        identity = "package: name='local.voice' versionCode='6' versionName='0.6.0'\n"
        with patch('make_release.command', side_effect=[signature, '', identity]) as commands:
            self.assertEqual(6, inspect_apk(self.apk, self.tools, CERTIFICATE))
            self.assertEqual(['apksigner', 'zipalign', 'aapt'], [call.args[0][0].name for call in commands.call_args_list])
        cases = [(signature.replace(CERTIFICATE, 'b' * 64), '', identity),
                 (signature + signature.replace('#1', '#2'), '', identity),
                 (signature, '', identity.replace('local.voice', 'foreign.app')),
                 (signature, '', identity.replace("versionCode='6'", "versionCode='0'"))]
        for values in cases:
            with patch('make_release.command', side_effect=values):
                with self.assertRaises(ValueError): inspect_apk(self.apk, self.tools, CERTIFICATE)
        with patch('make_release.command', side_effect=[signature, subprocess.CalledProcessError(1, ['zipalign'])]):
            with self.assertRaises(subprocess.SubprocessError): inspect_apk(self.apk, self.tools, CERTIFICATE)


if __name__ == '__main__': unittest.main()
