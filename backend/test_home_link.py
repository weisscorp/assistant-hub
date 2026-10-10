import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer
from bridge import handler
from home_link import HomeLinkStore, RevisionConflict


def key(): return str(uuid.uuid4())


def source():
    return dict(id=key(), name='Доставка', kind='WEB', value='https://example.test/private?token=fixture', participant='Участник 1', aliases=['курьер'], savedAt=1,
                instructions='Читай последнее наблюдение', followLinks=False, readerRecipe='', imageSha256='', observation=None)


def snapshot(revision=1, sources=None):
    return dict(version=1, revision=revision, sources=sources or [], people=[dict(alias='коллега', name='Тестовый контакт', telegram='@fixture')], note=None)


def event():
    return dict(id=key(), kind='message', source='Slack', title='Важно', body='Посмотри письмо', priority='important', createdAt=int(time.time()*1000))


class HomeLinkTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = HomeLinkStore(Path(self.temp.name) / 'private')
        self.device = key()
        self.store.sync(self.device, snapshot(sources=[source()]))

    def tearDown(self): self.temp.cleanup()

    def test_snapshot_survives_restart_and_deletions_replace_remote_data(self):
        old = self.store.knowledge(self.device)
        self.assertEqual({'revision': 1}, self.store.sync(self.device, old))
        other = HomeLinkStore(self.store.directory)
        self.assertEqual(old, other.knowledge(self.device))
        with self.assertRaises(RevisionConflict): self.store.sync(self.device, snapshot(1))
        self.store.sync(self.device, snapshot(2))
        self.assertEqual([], other.knowledge(self.device)['sources'])
        with self.assertRaises(RevisionConflict): self.store.sync(self.device, old)
        self.assertEqual(0o700, self.store.directory.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.store.path.stat().st_mode & 0o777)

    def test_image_bytes_are_scoped_verified_and_removed_with_source(self):
        image = b'\x89PNG\r\n\x1a\nfixture-image'; sha = hashlib.sha256(image).hexdigest()
        entry = source(); entry.update(kind='IMAGE', imageSha256=sha)
        with self.assertRaises(ValueError): self.store.sync(self.device, snapshot(2, [entry]))
        with self.assertRaises(ValueError): self.store.image(self.device, sha, b'wrong')
        self.store.image(self.device, sha, image)
        self.store.sync(self.device, snapshot(2, [entry]))
        self.assertEqual(image, self.store.image(self.device, sha))
        exported = Path(self.store.export_image(self.device, sha)); self.assertEqual(image, exported.read_bytes())
        with self.assertRaises(KeyError): self.store.image(key(), sha)
        self.store.sync(self.device, snapshot(3))
        self.assertFalse(exported.exists())
        with self.assertRaises(KeyError): self.store.image(self.device, sha)

    def test_events_replay_after_restart_are_idempotent_and_ack_is_not_read(self):
        value = event(); first = self.store.publish(self.device, value)
        self.assertEqual(first, self.store.publish(self.device, dict(value, createdAt=value['createdAt']+50)))
        with self.assertRaises(ValueError): self.store.publish(self.device, dict(value, body='different'))
        other = HomeLinkStore(self.store.directory)
        batch = other.events(self.device)
        self.assertEqual(1, len(batch['events']))
        self.assertEqual(value['body'], batch['events'][0]['body'])
        self.store.ack(self.device, first['seq'])
        self.assertEqual(1, len(other.events(self.device)['events']))
        self.assertEqual([], other.events(self.device, after=first['seq'])['events'])
        with self.assertRaises(KeyError): other.events(key())

    def test_long_poll_wakes_for_publication_and_does_not_drop_offline_messages(self):
        done = []
        thread = threading.Thread(target=lambda: done.append(self.store.events(self.device, wait=2)))
        thread.start(); time.sleep(.1)
        value = event(); self.store.publish(self.device, value)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(value['id'], done[0]['events'][0]['id'])

    def test_invalid_event_cannot_add_phone_actions_or_update_url(self):
        for bad in [dict(event(), action={'tool': 'install'}), dict(event(), url='https://evil.test'), dict(event(), body='x'*4001), dict(event(), kind='install')]:
            with self.assertRaises(ValueError): self.store.publish(self.device, bad)
        with self.assertRaises(KeyError): self.store.publish(key(), event())

    def test_long_poll_detects_publication_from_a_separate_store(self):
        from unittest.mock import patch
        waiting = threading.Event()
        results = []
        real_wait = self.store.changed.wait
        def observed_wait(timeout=None):
            waiting.set()
            return real_wait(timeout)
        with patch.object(self.store.changed, 'wait', side_effect=observed_wait):
            receiver = threading.Thread(target=lambda: results.append(self.store.events(self.device, wait=4)))
            receiver.start()
            try:
                self.assertTrue(waiting.wait(1), 'Receiver never reached long poll')
                separate = HomeLinkStore(self.store.directory)
                value = event()
                separate.publish(self.device, value)
                receiver.join(2)
                self.assertFalse(receiver.is_alive(), 'External publisher waited for the full long-poll timeout')
                self.assertEqual(value['id'], results[0]['events'][0]['id'])
            finally:
                receiver.join(5)

    def test_endpoint_auth_limits_and_device_scoping(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler('fixture', [], Path(self.temp.name), link=self.store))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def request(method, path, value=None, token='fixture'):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request(method, path, json.dumps(value) if value is not None else None, {'Authorization': 'Bearer '+token})
            response = connection.getresponse(); result = response.status, json.loads(response.read()); connection.close(); return result
        root = '/link/'+self.device
        try:
            for method, route in [('POST','/knowledge'), ('POST','/events'), ('POST','/ack'), ('GET','/knowledge'), ('GET','/events?after=0&wait=0'), ('GET','/image/'+'a'*64)]:
                self.assertEqual(401, request(method, root+route, event() if method=='POST' else None, token='wrong')[0])
            self.assertEqual(200, request('POST', root+'/events', event())[0])
            self.assertEqual(1, len(request('GET', root+'/events?after=0&wait=0')[1]['events']))
            self.assertEqual(400, request('GET', root+'/events?after=0&wait=26')[0])
            self.assertEqual(400, request('GET', root+'/events?after=0&after=1')[0])
            self.assertEqual(404, request('GET', '/link/'+key()+'/knowledge')[0])
            self.assertEqual(409, request('POST', root+'/knowledge', snapshot())[0])
            self.assertEqual(200, request('POST', root+'/knowledge', snapshot(2))[0])
            self.assertEqual([], request('GET', root+'/knowledge')[1]['sources'])
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_verified_release_queues_one_notice_without_download_or_install(self):
        directory = Path(self.temp.name)
        artifact = directory / 'release.apk'; artifact.write_bytes(b'fixture release')
        (directory / 'manifest.json').write_text(json.dumps(dict(file=artifact.name, versionCode=100, versionName='fixture', sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())))
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler('fixture', [], directory, link=self.store))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def poll(installed):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            conn.request('GET', '/link/'+self.device+'/events?after=0&wait=0', headers={'Authorization': 'Bearer fixture', 'X-Assistant-Version-Code': str(installed)})
            response = conn.getresponse(); self.assertEqual(200, response.status); value = json.loads(response.read()); conn.close(); return value
        try:
            self.assertEqual([], poll(100)['events'])
            first = poll(99)['events']; self.assertEqual(1, len(first)); self.assertEqual('update', first[0]['kind'])
            self.assertEqual(first, poll(99)['events'])
            self.assertNotIn('action', first[0]); self.assertNotIn('url', first[0])
            artifact.write_bytes(b'bad artifact')
            broken = json.loads((directory / 'manifest.json').read_text()); broken['versionCode'] = 101
            (directory / 'manifest.json').write_text(json.dumps(broken))
            # A newer manifest with stale checksum must not generate a notice.
            self.assertEqual(first, poll(99)['events'])
        finally: server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
