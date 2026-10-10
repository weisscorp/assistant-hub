import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from build_release import build_release, BUILD_TASKS, checkout_lock, BuildChecksFailed, build_failure_report


class BuildReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.keystore = self.root / 'external.jks'
        self.keystore.write_bytes(b'fake keystore, never used by Android tools')
        self.tools = self.root / 'build-tools'
        self.unsigned = self.root / 'app/build/outputs/apk/release/app-release-unsigned.apk'
        self.log = self.root / 'build.log'
        self.environment = patch.dict(os.environ, {'KEYSTORE_PASSWORD': 'test-ks-secret', 'KEY_PASSWORD': 'test-key-secret',
                                                  'ASSISTANT_TOKEN': 'test-bridge-secret', 'TLS_KEY': '/private/test.key'})
        self.environment.start()

    def tearDown(self):
        self.environment.stop(); self.temp.cleanup()

    def run_build(self):
        return build_release(self.root, self.tools, self.keystore, 'release', 'a' * 64,
                             self.root / 'releases', self.log)

    def test_fixed_build_tasks_signing_secret_boundary_and_publication_order(self):
        def execute(argv, **options):
            if Path(argv[0]).name == 'gradlew':
                self.unsigned.parent.mkdir(parents=True); self.unsigned.write_bytes(b'unsigned')
            elif Path(argv[0]).name == 'zipalign':
                Path(argv[-1]).write_bytes(b'aligned')
            else:
                Path(argv[argv.index('--out') + 1]).write_bytes(b'signed')
        with patch('build_release.subprocess.run', side_effect=execute) as run, patch('build_release.stage', return_value={'versionCode': 6}) as publish:
            self.assertEqual({'versionCode': 6}, self.run_build())
            calls = run.call_args_list
            self.assertEqual([str(self.root / 'gradlew'), *BUILD_TASKS, '--no-daemon'], calls[0].args[0])
            for name in ('KEYSTORE_PASSWORD', 'KEY_PASSWORD', 'ASSISTANT_TOKEN', 'TLS_KEY'):
                self.assertNotIn(name, calls[0].kwargs['env'])
            self.assertFalse(calls[0].kwargs['shell'])
            self.assertEqual('test-ks-secret', calls[2].kwargs['env']['KEYSTORE_PASSWORD'])
            self.assertEqual('test-key-secret', calls[2].kwargs['env']['KEY_PASSWORD'])
            self.assertNotIn('test-ks-secret', calls[2].args[0])
            self.assertNotIn('test-key-secret', calls[2].args[0])
            self.assertNotIn('ASSISTANT_TOKEN', calls[2].kwargs['env'])
            publish.assert_called_once()
            self.assertFalse(publish.call_args.args[0].exists())  # temporary signed APK cleaned up

    def test_failed_build_cannot_sign_or_publish_stale_artifact(self):
        self.unsigned.parent.mkdir(parents=True); self.unsigned.write_bytes(b'stale APK')
        with patch('build_release.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['gradlew'])) as run, patch('build_release.stage') as publish:
            with self.assertRaises(subprocess.SubprocessError): self.run_build()
            self.assertEqual(1, run.call_count)
            publish.assert_not_called()
        self.assertFalse(self.unsigned.exists())

    def test_missing_expected_output_cannot_be_published(self):
        with patch('build_release.subprocess.run') as run, patch('build_release.stage') as publish:
            with self.assertRaises(ValueError): self.run_build()
            self.assertEqual(1, run.call_count)
            publish.assert_not_called()

    def test_assigned_version_is_passed_to_gradle_and_asserted_after_signing(self):
        def execute(argv, **options):
            if Path(argv[0]).name == 'gradlew':
                self.unsigned.parent.mkdir(parents=True); self.unsigned.write_bytes(b'unsigned')
            elif Path(argv[0]).name == 'zipalign': Path(argv[-1]).write_bytes(b'aligned')
            else: Path(argv[argv.index('--out') + 1]).write_bytes(b'signed')
        with patch('build_release.subprocess.run', side_effect=execute) as run, patch('build_release.stage', return_value={'versionCode': 8}) as publish:
            build_release(self.root, self.tools, self.keystore, 'release', 'a' * 64, self.root / 'releases', self.log, 8)
            self.assertIn('-PhelmetVersionCode=8', run.call_args_list[0].args[0])
            self.assertIn('-PhelmetVersionName=0.8.0', run.call_args_list[0].args[0])
            self.assertEqual(8, publish.call_args.kwargs['version_code'])
        with self.assertRaises(ValueError):
            build_release(self.root, self.tools, self.keystore, 'release', 'a' * 64, self.root / 'releases', self.log, True)

    def test_gradle_privileges_are_dropped_and_signer_does_not_drop_identity(self):
        identity = SimpleNamespace(pw_uid=12001, pw_gid=12001, pw_dir='/srv/helmet-build-home', pw_name='helmet-build')
        actual_fstat = os.fstat
        def root_owned(descriptor):
            values = list(actual_fstat(descriptor)); values[4] = 0
            return os.stat_result(values)
        def execute(argv, **options):
            if Path(argv[0]).name == 'gradlew':
                self.unsigned.parent.mkdir(parents=True); self.unsigned.write_bytes(b'unsigned')
            elif Path(argv[0]).name == 'zipalign': Path(argv[-1]).write_bytes(b'aligned')
            else: Path(argv[argv.index('--out') + 1]).write_bytes(b'signed')
        with patch('build_release.pwd.getpwnam', return_value=identity), patch('build_release.os.geteuid', return_value=0), \
                patch('build_release.os.fstat', side_effect=root_owned), \
                patch('build_release.os.fchown') as change_owner, \
                patch('build_release.subprocess.run', side_effect=execute) as run, patch('build_release.stage'):
            build_release(self.root, self.tools, self.keystore, 'release', 'a' * 64, self.root / 'releases', self.log, 8, 'helmet-build')
            build = run.call_args_list[0]
            self.assertEqual((12001,12001,[]), (build.kwargs['user'],build.kwargs['group'],build.kwargs['extra_groups']))
            self.assertEqual('/srv/helmet-build-home', build.kwargs['env']['HOME'])
            self.assertNotIn('KEYSTORE_PASSWORD', build.kwargs['env'])
            self.assertNotIn('user', run.call_args_list[2].kwargs)
            change_owner.assert_called_once()
        with patch('build_release.pwd.getpwnam', return_value=identity), patch('build_release.os.geteuid', return_value=12001):
            with self.assertRaises(ValueError):
                build_release(self.root, self.tools, self.keystore, 'release', 'a' * 64, self.root / 'releases', self.log, 8, 'helmet-build')

    def test_checkout_lock_rejects_symlink_and_hardlink(self):
        original = self.root / 'original'; original.write_text('unchanged')
        lock = self.root / 'lock'
        lock.symlink_to(original)
        with self.assertRaises(OSError):
            with checkout_lock(lock): pass
        lock.unlink(); os.link(original, lock)
        with self.assertRaises(ValueError):
            with checkout_lock(lock): pass
        self.assertEqual('unchanged', original.read_text())

    def test_only_failed_build_checks_get_bounded_diagnostics(self):
        self.log.write_text('x' * 20000 + '\x1b[31me: compile error\x1b[0m\n')
        failure = BuildChecksFailed(subprocess.CalledProcessError(1, ['gradlew']), self.log)
        report = build_failure_report(failure, 8)
        self.assertEqual('build_checks_failed', report['kind'])
        self.assertEqual(8, report['versionCode'])
        self.assertLessEqual(len(report['diagnostics']), 4000)
        self.assertIn('e: compile error', report['diagnostics'])
        self.assertNotIn('\x1b', report['diagnostics'])
        self.assertIsNone(build_failure_report(subprocess.CalledProcessError(1, ['apksigner']), 8))


if __name__ == '__main__': unittest.main()
