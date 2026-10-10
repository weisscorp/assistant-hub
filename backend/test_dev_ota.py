import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from dev_ota import Artifacts, atomic_json, build_once, handler, load


class DevOtaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.output = self.root / 'releases'; self.output.mkdir()
        self.token = 'synthetic-' + 'x' * 40
    def tearDown(self): self.temp.cleanup()
    def artifact(self, version, content=b'synthetic apk'):
        sha = hashlib.sha256(content).hexdigest(); name = f'helmet-{version}-{sha[:16]}.apk'
        file = self.output / name; file.write_bytes(content); file.chmod(0o444)
        manifest = dict(versionCode=version, file=name, sha256=sha, packageName='local.voice', certificateSha256='a' * 64)
        atomic_json(self.output / 'manifest.json', manifest)
        return manifest
    def test_requires_bearer_and_serves_previous_snapshot_during_publication(self):
        old = self.artifact(80); server = ThreadingHTTPServer(('127.0.0.1', 0), handler(Artifacts(self.output), self.token))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            def request(path, token=None):
                conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                conn.request('GET', path, headers={'Authorization': 'Bearer ' + token} if token else {})
                response = conn.getresponse(); body = response.read(); conn.close(); return response.status, body
            self.assertEqual(401, request('/updates/manifest')[0])
            self.assertEqual(401, request('/updates/manifest', 'wrong')[0])
            code, body = request('/updates/manifest', self.token)
            self.assertEqual(200, code); self.assertEqual(13, json.loads(body)['sizeBytes'])
            self.artifact(81, b'next synthetic apk')
            self.assertEqual((200, b'synthetic apk'), request('/updates/' + old['file'], self.token))
            for route in ['/updates/../manifest.json', '/updates/%2e%2e/token', '/updates/manifest?token=x', '/conversation']:
                self.assertEqual(404, request(route, self.token)[0])
        finally: server.shutdown(); server.server_close(); thread.join()
    def test_artifact_integrity_symlink_and_writable_file_rejected(self):
        manifest = self.artifact(80); store = Artifacts(self.output)
        self.assertEqual(80, store.manifest()['versionCode'])
        file = self.output / manifest['file']; file.chmod(0o644)
        with self.assertRaises(ValueError): store.manifest()
        file.write_bytes(b'changed bytes'); file.chmod(0o444)
        with self.assertRaises(ValueError): store.manifest()
        file.unlink(); file.symlink_to(self.root / 'outside')
        with self.assertRaises(OSError): store.manifest()
    def test_manifest_bound_and_monotonic_identity(self):
        manifest = self.artifact(80); manifest['versionCode'] = 81
        atomic_json(self.output / 'manifest.json', manifest)
        with self.assertRaises(ValueError): Artifacts(self.output).manifest()
        (self.output / 'manifest.json').write_bytes(b'x' * 16385)
        with self.assertRaises(ValueError): Artifacts(self.output).manifest()
    def test_private_configuration_mode_is_enforced(self):
        file = self.root / 'private.json'; atomic_json(file, {'fixture': True})
        self.assertTrue(load(file)['fixture']); file.chmod(0o644)
        with self.assertRaises(ValueError): load(file)
        file.chmod(0o600); link = self.root / 'link'; link.symlink_to(file)
        with self.assertRaises(ValueError): load(link)
    def repo_config(self):
        source = self.root / 'checkout'; source.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'config', 'user.name', 'Synthetic fixture'], check=True)
        subprocess.run(['git', '-C', str(source), 'config', 'user.email', 'fixture@example.invalid'], check=True)
        (source / 'dependencies.json').write_text('{}')
        subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(source), 'commit', '-qm', 'fixture'], check=True)
        inputs = self.root / 'inputs'; inputs.mkdir()
        (inputs / 'dependencies.sha256').write_text(hashlib.sha256(b'{}').hexdigest())
        secrets = self.root / 'credentials.json'; atomic_json(secrets, {'KEYSTORE_PASSWORD': 'fixture', 'KEY_PASSWORD': 'fixture'})
        return dict(checkout=str(source), workspace=str(self.root / 'worker'), output=str(self.output), minimum_version=79,
                    prepared_inputs=str(inputs), credentials_file=str(secrets), java_home='/fixture/java', android_home='/fixture/sdk',
                    build_tools='/fixture/tools', keystore='/fixture/key', alias='fixture', certificate_sha256='a' * 64)
    def test_build_failure_keeps_old_release_and_retries_reserved_version_then_deduplicates(self):
        self.artifact(79); config = self.repo_config(); environment = dict(os.environ)
        seen = []
        def fail(*args): seen.append(args[-1]); self.assertNotIn('UNRELATED_SECRET', os.environ); raise ValueError('synthetic failed build')
        with patch.dict(os.environ, UNRELATED_SECRET='fixture'):
            with self.assertRaises(ValueError): build_once(config, builder=fail)
        self.assertEqual(environment, dict(os.environ)); self.assertEqual([80], seen)
        self.assertEqual(79, Artifacts(self.output).manifest()['versionCode'])
        state = Path(config['workspace']) / 'build-state.json'; value = json.loads(state.read_text()); value['retry_after'] = 0; atomic_json(state, value)
        def success(*args): seen.append(args[-1]); return self.artifact(args[-1], b'published fixture')
        self.assertEqual(80, build_once(config, builder=success)['versionCode'])
        self.assertIsNone(build_once(config, builder=fail)); self.assertEqual([80, 80], seen)
        # Publication receipt lost after a crash: recover without rebuilding.
        value = json.loads(state.read_text()); value.pop('published_commit'); atomic_json(state, value)
        self.assertIsNone(build_once(config, builder=fail))
    def test_dirty_worktree_and_dependency_mismatch_never_call_builder(self):
        config = self.repo_config(); source = Path(config['checkout']); (source / 'dependencies.json').write_text('{"changed":true}')
        with self.assertRaises(ValueError): build_once(config, builder=lambda *args: self.fail('Must not build dirty source'))
        subprocess.run(['git', '-C', str(source), 'commit', '-qam', 'changed dependencies'], check=True)
        with self.assertRaises(ValueError): build_once(config, builder=lambda *args: self.fail('Must not build mismatched inputs'))


if __name__ == '__main__': unittest.main()
