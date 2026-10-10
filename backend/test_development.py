from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from development_agent import fingerprint, perform, verify_publication, repair_diagnostics


class DevelopmentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'app/src/main/example.txt'
        self.source.parent.mkdir(parents=True); self.source.write_text('before')
        self.output = self.root / 'releases'
        self.config = {'agent_command': ['fixed-agent'], 'release_command': ['trusted-release'],
                       'workspace': str(self.root), 'release_dir': str(self.output), 'build_tools': str(self.root),
                       'certificate_sha256': 'a' * 64, 'minimum_version': 7}

    def tearDown(self): self.temp.cleanup()

    def publish(self, version=8):
        self.output.mkdir(exist_ok=True)
        data = b'fixture APK bytes, not Android-signed'
        checksum = hashlib.sha256(data).hexdigest()
        name = f'helmet-{version}-{checksum[:16]}.apk'
        (self.output / name).write_bytes(data)
        manifest = {'versionCode': version, 'file': name, 'sha256': checksum,
                    'packageName': 'local.voice', 'certificateSha256': 'a' * 64}
        (self.output / 'manifest.json').write_text(json.dumps(manifest))
        return manifest

    def test_only_explicit_request_can_release_and_text_never_enters_argv(self):
        with patch('development_agent.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
            perform(self.config, 'обычная задача $(literal)')
            self.assertEqual(1, run.call_count)
            self.assertEqual(['fixed-agent'], run.call_args.args[0])
            self.assertEqual('обычная задача $(literal)', run.call_args.kwargs['input'])
        for text in ('разработай обновление помощника', 'разработай обновление помощника '):
            with patch('development_agent.subprocess.run') as run:
                with self.assertRaises(ValueError): perform(self.config, text)
                run.assert_not_called()

    def test_verified_release_and_secret_boundary(self):
        def execute(argv, **options):
            if argv == ['fixed-agent']:
                self.source.write_text('after')
                return subprocess.CompletedProcess(argv, 0, 'Проверил.', '')
            self.publish()
            return subprocess.CompletedProcess(argv, 0)
        with patch.dict(os.environ, {'KEYSTORE_PASSWORD': 'fixture-sign-secret', 'HELMET_MODEL_API_KEY': 'fixture-model-secret'}), \
                patch('development_agent.subprocess.run', side_effect=execute) as run, \
                patch('development_agent.inspect_apk', return_value=8) as inspect, redirect_stdout(io.StringIO()) as output:
            perform(self.config, 'разработай обновление помощника $(literal); `literal`')
            self.assertIn('Выпущено обновление 8', output.getvalue())
            self.assertEqual(['trusted-release', '--version-code', '8'], run.call_args_list[1].args[0])
            self.assertIn('$(literal); `literal`', run.call_args_list[0].kwargs['input'])
            for call in run.call_args_list: self.assertNotIn('KEYSTORE_PASSWORD', call.kwargs['env'])
            self.assertIn('HELMET_MODEL_API_KEY', run.call_args_list[0].kwargs['env'])
            self.assertNotIn('HELMET_MODEL_API_KEY', run.call_args_list[1].kwargs['env'])
            inspect.assert_called_once()

    def test_no_change_agent_failure_and_build_failure_do_not_report_publication(self):
        with patch('development_agent.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'Готово.', '')) as run, redirect_stdout(io.StringIO()) as output:
            perform(self.config, 'разработай обновление помощника проверь')
            self.assertEqual(1, run.call_count)
            self.assertIn('Обновление не выпускалось', output.getvalue())
        with patch('development_agent.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['agent'])) as run:
            with self.assertRaises(subprocess.CalledProcessError): perform(self.config, 'разработай обновление помощника исправь')
            self.assertEqual(1, run.call_count)
        def failed(argv, **options):
            if argv == ['fixed-agent']:
                self.source.write_text('changed')
                return subprocess.CompletedProcess(argv, 0, 'Готово.', '')
            raise subprocess.CalledProcessError(1, argv)
        with patch('development_agent.subprocess.run', side_effect=failed), redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(subprocess.CalledProcessError): perform(self.config, 'разработай обновление помощника исправь')
            self.assertEqual('', output.getvalue())
        self.assertFalse((self.output / 'manifest.json').exists())

    def test_receipt_requires_actual_artifact_hash_identity_version_and_signature(self):
        manifest = self.publish()
        with patch('development_agent.inspect_apk', return_value=8):
            self.assertEqual(manifest, verify_publication(self.output, self.root, 'a' * 64, 8))
            with self.assertRaises(ValueError): verify_publication(self.output, self.root, 'a' * 64, 9)
            (self.output / manifest['file']).write_bytes(b'corrupted')
            with self.assertRaises(ValueError): verify_publication(self.output, self.root, 'a' * 64, 8)
        self.publish()
        with patch('development_agent.inspect_apk', side_effect=ValueError('wrong signature')):
            with self.assertRaises(ValueError): verify_publication(self.output, self.root, 'a' * 64, 8)
        self.source.unlink(); self.source.symlink_to('/outside/source')
        with self.assertRaises(ValueError): fingerprint(self.root)

    def test_build_diagnostics_allow_one_source_repair_and_same_version_retry(self):
        calls = []
        report = json.dumps({'kind': 'build_checks_failed', 'versionCode': 8, 'diagnostics': 'e: source.kt syntax error'})
        def execute(argv, **options):
            calls.append((argv, options))
            if argv == ['fixed-agent']:
                self.source.write_text('repaired' if 'Диагностика проверок:' in options['input'] else 'broken')
                return subprocess.CompletedProcess(argv, 0, 'Исправил.', '')
            if self.source.read_text() == 'broken': return subprocess.CompletedProcess(argv, 1, report, '')
            self.publish()
            return subprocess.CompletedProcess(argv, 0, '{}', '')
        with patch('development_agent.subprocess.run', side_effect=execute), patch('development_agent.inspect_apk', return_value=8), redirect_stdout(io.StringIO()) as output:
            perform(self.config, 'разработай обновление помощника добавь функцию')
            self.assertIn('Выпущено обновление 8', output.getvalue())
        self.assertEqual(4, len(calls))
        self.assertEqual(calls[1][0], calls[3][0])
        self.assertIn('syntax error', calls[2][1]['input'])
        self.assertIn('недоверенный вывод', calls[2][1]['input'])

    def test_repair_limit_and_untrusted_failure_reports(self):
        for data in ({'kind': 'signature_failed', 'versionCode': 8, 'diagnostics': 'x'},
                     {'kind': 'build_checks_failed', 'versionCode': True, 'diagnostics': 'x'},
                     {'kind': 'build_checks_failed', 'versionCode': 9, 'diagnostics': 'x'},
                     {'kind': 'build_checks_failed', 'versionCode': 8, 'diagnostics': 'x' * 4001}):
            self.assertIsNone(repair_diagnostics(subprocess.CompletedProcess([], 1, json.dumps(data)), 8))
        self.assertIsNone(repair_diagnostics(subprocess.CompletedProcess([], 1, 'not JSON'), 8))
        count = [0]
        def execute(argv, **options):
            count[0] += 1
            if argv == ['fixed-agent']:
                self.source.write_text(f'still broken {count[0]}')
                return subprocess.CompletedProcess(argv, 0, 'Исправил.', '')
            return subprocess.CompletedProcess(argv, 1, json.dumps({'kind': 'build_checks_failed', 'versionCode': 8, 'diagnostics': 'error'}), '')
        with patch('development_agent.subprocess.run', side_effect=execute), redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(subprocess.CalledProcessError): perform(self.config, 'разработай обновление помощника функция')
            self.assertEqual('', output.getvalue())
        self.assertEqual(4, count[0])
        self.assertFalse((self.output / 'manifest.json').exists())

    def test_changed_publication_and_exhausted_budget_prevent_repair(self):
        count = [0]
        def execute(argv, **options):
            count[0] += 1
            if argv == ['fixed-agent']:
                self.source.write_text('broken')
                return subprocess.CompletedProcess(argv, 0, 'Изменил.', '')
            self.publish()
            return subprocess.CompletedProcess(argv, 1, json.dumps({'kind': 'build_checks_failed', 'versionCode': 8, 'diagnostics': 'error'}), '')
        with patch('development_agent.subprocess.run', side_effect=execute), patch('development_agent.inspect_apk', return_value=8), redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(ValueError): perform(self.config, 'разработай обновление помощника функция')
            self.assertEqual('', output.getvalue())
        self.assertEqual(2, count[0])
    def test_budget_exhaustion_prevents_agent_start(self):
        self.config['workflow_seconds'] = 60
        with patch('development_agent.time.monotonic', side_effect=[0, 61]), patch('development_agent.subprocess.run') as run:
            with self.assertRaises(TimeoutError): perform(self.config, 'разработай обновление помощника функция')
            run.assert_not_called()


if __name__ == '__main__': unittest.main()
