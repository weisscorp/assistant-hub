"""Synthetic real HTTP/TLS upload and durable worker forwarding regressions."""
from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import unittest
from unittest.mock import patch

from dev_ota import Artifacts, Publisher, atomic_json, build_once, handler
from dev_ota_container import configuration
from dev_ota_publish import publish, RemotePublishError
from make_release import MAX_APK
import test_dev_ota as existing_tests


class UploadTest(unittest.TestCase):
    """Reuse temporary synthetic releases; no phone or real APK is touched."""
    publisher_token = 'synthetic-publisher-' + 'y' * 40
    setUp = existing_tests.DevOtaTest.setUp
    tearDown = existing_tests.DevOtaTest.tearDown
    artifact = existing_tests.DevOtaTest.artifact
    repo_config = existing_tests.DevOtaTest.repo_config

    @contextmanager
    def service(self, tls=None, enable_publication=True):
        publisher = Publisher(self.publisher_token, self.root / 'tools', 'a' * 64) if enable_publication else None
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(Artifacts(self.output), self.token, publisher))
        server.daemon_threads = True
        if tls: server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try: yield server
        finally: server.shutdown(); server.server_close(); thread.join()

    def request(self, server, method='POST', path='/updates/publish', body=b'next synthetic APK', token=None, headers=None):
        metadata = {'Content-Length': str(len(body)), 'X-OTA-Version-Code': '81',
                    'X-OTA-SHA256': hashlib.sha256(body).hexdigest()}
        if token: metadata['Authorization'] = 'Bearer ' + token
        if headers: metadata.update(headers)
        conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body if method == 'POST' else None,
                         headers=metadata if method == 'POST' else ({'Authorization': 'Bearer ' + token} if token else {}))
            response = conn.getresponse(); return response.status, json.loads(response.read())
        finally: conn.close()

    def test_health_is_public_but_upload_key_is_separate_and_opt_in(self):
        self.artifact(80)
        with self.service() as server:
            self.assertEqual((200, {'status': 'ok'}), self.request(server, 'GET', '/healthz'))
            self.assertEqual(401, self.request(server, token=self.token)[0])
            self.assertEqual(401, self.request(server)[0])
            self.assertEqual(200, self.request(server, 'GET', '/updates/manifest', token=self.publisher_token)[0])
            self.assertEqual(403, self.request(server, 'GET', '/updates/' + Artifacts(self.output).manifest()['file'], token=self.publisher_token)[0])
            self.assertEqual(405, self.request(server, path='/conversation', token=self.publisher_token)[0])
        with self.service(enable_publication=False) as server:
            self.assertEqual(405, self.request(server, token=self.publisher_token)[0])
        with self.assertRaises(ValueError): handler(Artifacts(self.output), self.token, Publisher(self.token, self.root, 'a' * 64))
        self.assertEqual(80, Artifacts(self.output).manifest()['versionCode'])

    def test_validated_upload_exact_retry_and_old_artifact_download(self):
        previous = self.artifact(80)
        with self.service() as server, patch('make_release.inspect_apk', return_value=81) as inspect:
            code, first = self.request(server, token=self.publisher_token)
            self.assertEqual(200, code); self.assertEqual(81, first['versionCode'])
            file = self.output / first['file']; inode = file.stat().st_ino
            self.assertEqual(0o444, file.stat().st_mode & 0o777)
            self.assertEqual((200, first), self.request(server, token=self.publisher_token))
            self.assertEqual(inode, file.stat().st_ino); self.assertEqual(2, inspect.call_count)
            self.assertEqual(b'synthetic apk', (self.output / previous['file']).read_bytes())
        self.assertFalse(list(self.output.glob('.upload-*'))); self.assertFalse(list(self.output.glob('.release-*')))

    def test_bad_hash_version_and_native_validation_preserve_previous_manifest(self):
        previous = self.artifact(80)
        with self.service() as server:
            with patch('make_release.inspect_apk') as inspect:
                self.assertEqual(422, self.request(server, token=self.publisher_token, headers={'X-OTA-SHA256': 'f' * 64})[0])
                inspect.assert_not_called()
            with patch('make_release.inspect_apk', return_value=82):
                self.assertEqual(422, self.request(server, token=self.publisher_token)[0])
            for failure in (ValueError('wrong signing certificate'), ValueError('foreign package'),
                            subprocess.CalledProcessError(1, ['zipalign'])):
                with patch('make_release.inspect_apk', side_effect=failure):
                    self.assertEqual((422, {'error': 'release_rejected'}), self.request(server, token=self.publisher_token))
        self.assertEqual(previous, json.loads((self.output / 'manifest.json').read_text()))
        self.assertEqual(1, len(list(self.output.glob('*.apk'))))
        self.assertFalse(list(self.output.glob('.upload-*')))

    def test_downgrade_and_conflicting_version_are_rejected(self):
        previous = self.artifact(80)
        with self.service() as server:
            for version in (79, 80):
                with patch('make_release.inspect_apk', return_value=version):
                    code, body = self.request(server, token=self.publisher_token, headers={'X-OTA-Version-Code': str(version)})
                    self.assertEqual((409, {'error': 'release_conflict'}), (code, body))
        self.assertEqual(previous, json.loads((self.output / 'manifest.json').read_text()))

    def test_ambiguous_duplicate_and_bounded_metadata_rejected_before_writing(self):
        self.artifact(80)
        with self.service() as server, patch('dev_ota.stage') as stage:
            for changes in ({'Transfer-Encoding': 'chunked'}, {'Transfer-Encoding': ''}, {'Content-Length': str(MAX_APK + 1)},
                            {'Content-Length': '0'}, {'Content-Length': '-1'}, {'Content-Length': '01'},
                            {'X-OTA-Version-Code': '2100000001'}, {'X-OTA-SHA256': 'A' * 64}, {'Expect': '100-continue'}):
                self.assertEqual(400, self.request(server, token=self.publisher_token, headers=changes)[0])
            for duplicate in ('Authorization', 'Content-Length', 'X-OTA-Version-Code', 'X-OTA-SHA256', 'Host'):
                conn = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                conn.putrequest('POST', '/updates/publish', skip_host=True)
                headers = {'Host': 'localhost', 'Authorization': 'Bearer ' + self.publisher_token,
                           'Content-Length': '1', 'X-OTA-Version-Code': '81', 'X-OTA-SHA256': 'a' * 64}
                for name, value in headers.items(): conn.putheader(name, value)
                conn.putheader(duplicate, headers[duplicate]); conn.endheaders()
                response = conn.getresponse(); self.assertEqual(400, response.status); response.read(); conn.close()
            stage.assert_not_called()
        self.assertFalse(list(self.output.glob('.upload-*')))

    def test_interrupted_upload_cleans_private_candidate_and_keeps_release(self):
        previous = self.artifact(80)
        with self.service() as server, patch('dev_ota.stage') as stage:
            with socket.create_connection(('127.0.0.1', server.server_port), timeout=5) as sock:
                headers = ('POST /updates/publish HTTP/1.0\r\nHost: localhost\r\nAuthorization: Bearer ' + self.publisher_token +
                           '\r\nContent-Length: 200\r\nX-OTA-Version-Code: 81\r\nX-OTA-SHA256: ' + 'a' * 64 + '\r\n\r\n')
                sock.sendall(headers.encode() + b'short'); sock.shutdown(socket.SHUT_WR)
                data = b''
                while block := sock.recv(4096): data += block
                self.assertIn(b'400 Bad Request', data); self.assertIn(b'incomplete_upload', data)
            stage.assert_not_called()
        self.assertEqual(previous, json.loads((self.output / 'manifest.json').read_text()))
        self.assertFalse(list(self.output.glob('.upload-*')))

    def test_one_publication_at_a_time(self):
        self.artifact(80); ready = threading.Event(); finish = threading.Event(); results = []
        def inspect(*_):
            ready.set(); self.assertTrue(finish.wait(5)); return 81
        with self.service() as server, patch('make_release.inspect_apk', side_effect=inspect):
            first = threading.Thread(target=lambda: results.append(self.request(server, token=self.publisher_token)))
            first.start(); self.assertTrue(ready.wait(5))
            try: self.assertEqual((409, {'error': 'publication_busy'}), self.request(server, token=self.publisher_token))
            finally: finish.set(); first.join()
            self.assertEqual(200, results[0][0])

    def certificate(self, name):
        certificate = self.root / (name + '.pem'); key = self.root / (name + '.key')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=Synthetic OTA', '-addext', 'subjectAltName=IP:127.0.0.1',
                        '-keyout', str(key), '-out', str(certificate)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return certificate, key

    def test_https_client_streams_checked_release_verifies_certificate_and_recovers_receipt(self):
        local = self.root / 'local'; local.mkdir()
        content = b'synthetic signed release' * 10000; sha = hashlib.sha256(content).hexdigest()
        name = 'helmet-81-' + sha[:16] + '.apk'; artifact = local / name; artifact.write_bytes(content); artifact.chmod(0o444)
        atomic_json(local / 'manifest.json', dict(versionCode=81, file=name, sha256=sha, packageName='local.voice', certificateSha256='a' * 64))
        keyfile = self.root / 'publisher'; keyfile.write_text(self.publisher_token); keyfile.chmod(0o600)
        certificate, key = self.certificate('trusted'); other, _ = self.certificate('wrong')
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(certificate, key)
        with self.service(tls=tls) as server, patch('make_release.inspect_apk', return_value=81) as inspect:
            remote = dict(endpoint='https://127.0.0.1:' + str(server.server_port), token_file=str(keyfile), tls_cert=str(other))
            with self.assertRaises(ssl.SSLCertVerificationError): publish(local, remote, 'a' * 64)
            inspect.assert_not_called()
            remote['tls_cert'] = str(certificate)
            original_reply = server.RequestHandlerClass.reply
            def wrong_receipt(request, code, body):
                if request.command == 'POST' and code == 200: body = {**body, 'sha256': 'f' * 64}
                original_reply(request, code, body)
            with patch.object(server.RequestHandlerClass, 'reply', wrong_receipt):
                with self.assertRaisesRegex(RemotePublishError, 'receipt differs'):
                    publish(local, remote, 'a' * 64)
            # The server published atomically; a lost or invalid receipt recovers by
            # reading the checked manifest without re-uploading or re-validating.
            receipt = publish(local, remote, 'a' * 64)
            self.assertEqual(sha, receipt['sha256']); self.assertEqual(content, (self.output / name).read_bytes())
            self.assertEqual(receipt, publish(local, remote, 'a' * 64)); self.assertEqual(1, inspect.call_count)
            with self.assertRaises(RemotePublishError): publish(local, {**remote, 'endpoint': 'http://127.0.0.1'}, 'a' * 64)
            keyfile.chmod(0o644)
            with self.assertRaises(ValueError): publish(local, remote, 'a' * 64)

    def test_forward_retry_does_not_rebuild_published_clean_commit_even_with_dirty_checkout(self):
        config = self.repo_config(); config['remote_publication'] = dict(endpoint='https://127.0.0.1:8443')
        builds = []; receipt = self.artifact(80)
        def builder(*args): builds.append(args[-1]); return self.artifact(args[-1])
        with patch('dev_ota_publish.publish', side_effect=OSError('synthetic offline')):
            with self.assertRaises(OSError): build_once(config, builder=builder)
        self.assertEqual([], builds)  # First forward the initial checked release.
        state_path = Path(config['workspace']) / 'build-state.json'
        state = json.loads(state_path.read_text()); state['remote_retry_after'] = 0; atomic_json(state_path, state)
        with patch('dev_ota_publish.publish', side_effect=lambda *_: Artifacts(self.output).manifest()):
            build_once(config, builder=builder)
        self.assertEqual([81], builds)
        state = json.loads(state_path.read_text()); state.pop('remote_published_sha256'); state['remote_retry_after'] = 0
        atomic_json(state_path, state)
        # Dirty source still cannot build; pending immutable publication runs first.
        (Path(config['checkout']) / 'dependencies.json').write_text('{"changed":true}')
        with patch('dev_ota_publish.publish', return_value=Artifacts(self.output).manifest()) as publish_again:
            with self.assertRaises(ValueError): build_once(config, builder=lambda *_: self.fail('No rebuild'))
            publish_again.assert_called_once()
        self.assertEqual([81], builds)

    def test_container_copies_projected_keys_to_owned_private_files(self):
        secret = self.root / 'secret'; secret.mkdir(); backing = self.root / 'backing'; backing.mkdir()
        values = {'ota.key': self.token, 'publisher.key': self.publisher_token, 'tls.pem': 'synthetic public certificate',
                  'tls.key': 'synthetic private key', 'certificate.sha256': 'a' * 64}
        for name, value in values.items():
            (backing / name).write_text(value); (backing / name).chmod(0o440); (secret / name).symlink_to(backing / name)
        config = configuration({'POD_IP': '10.0.0.3', 'OTA_SECRET_DIR': str(secret), 'OTA_PRIVATE_DIR': str(self.root / 'private')})
        self.assertEqual('10.0.0.3', config['host']); self.assertEqual(8443, config['port'])
        self.assertEqual('/opt/android-build-tools', config['build_tools'])
        for name, value in values.items():
            target = self.root / 'private' / name
            self.assertEqual(value, target.read_text()); self.assertFalse(target.is_symlink())
            self.assertEqual(0o600, target.stat().st_mode & 0o777)


if __name__ == '__main__': unittest.main()
