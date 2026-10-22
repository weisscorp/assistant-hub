"""HTTP and temporary state, synthetic provider; no audio or devices."""
import unittest
import urllib.error
import uuid
import test_hub_stack as fixtures

class CoucouHubTests(unittest.TestCase):
    setUp = fixtures.HubTests.setUp
    tearDown = fixtures.HubTests.tearDown
    request = fixtures.HubTests.request
    wait = fixtures.HubTests.wait

    def scoped(self, body=None):
        return self.request('/coucou/synthetic-hub/api/desktop-hub', body, auth=False,
                            headers={'Authorization': 'Bearer ollama'})

    def test_chat_notifications_idempotency_and_topic_switch(self):
        self.app.config['coucou_capability'] = 'synthetic-hub'
        self.app.store.notify(self.context, 'synthetic', 'Fixture notice')
        self.app.store.switch('coucou', self.context, self.app.store.focus('coucou')['revision'])
        snapshot = self.scoped()
        self.assertEqual(snapshot['notifications'][0]['body'], 'Fixture notice')
        self.assertIn('fixture', snapshot['providers'])
        self.assertNotIn('memory', snapshot)
        body = {'action': 'send', 'context': self.context, 'provider': 'fixture',
                'prompt': 'Fixture question', 'id': str(uuid.uuid4())}
        row = self.scoped(body)
        self.wait(row['id'])
        self.assertEqual(self.scoped(body)['id'], row['id'])
        self.assertEqual(self.fixture.calls, 1)
        self.assertEqual(self.scoped()['history'][0]['body'], 'Fixture question')
        focus = self.scoped()['focus']
        self.scoped({'action': 'focus', 'context': self.other, 'revision': focus['revision']})
        self.assertEqual(self.scoped()['history'], [])
        self.assertEqual(self.scoped(body)['context_id'], self.context)
        self.assertEqual(self.scoped()['focus']['context_id'], self.other)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.scoped({'action': 'focus', 'context': self.context, 'revision': focus['revision']})
        self.assertEqual(error.exception.code, 409)

    def test_capability_does_not_open_owner_routes(self):
        with self.assertRaises(urllib.error.HTTPError): self.scoped()
        self.app.config['coucou_capability'] = 'synthetic-hub'
        for path in ['/api/desktop-hub', '/coucou/wrong/api/desktop-hub',
                     '/coucou/synthetic-hub/api/state', '/coucou/synthetic-hub/api/notify']:
            with self.assertRaises(urllib.error.HTTPError):
                self.request(path, auth=False, headers={'Authorization': 'Bearer ollama'})
        with self.assertRaises(urllib.error.HTTPError):
            self.scoped({'action': 'notify', 'body': 'unsupported'})
        with self.assertRaises(urllib.error.HTTPError):
            self.scoped({'action': 'send', 'context': self.context, 'provider': 'missing',
                         'prompt': 'Fixture', 'id': str(uuid.uuid4())})

    def test_foreign_origin_rejected(self):
        self.app.config['coucou_capability'] = 'synthetic-hub'
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/coucou/synthetic-hub/api/desktop-hub', auth=False,
                         headers={'Authorization': 'Bearer ollama', 'Origin': 'http://evil.invalid'})

    def test_provider_without_ready_has_stable_ui_schema(self):
        self.app.config['coucou_capability'] = 'synthetic-hub'
        self.app.statuses = lambda: {'dot': {'available': True, 'enabled': True},
                                    'offline': {'available': False},
                                    'fixture': {'ready': True, 'detail': 'Synthetic'}}
        providers = self.scoped()['providers']
        self.assertEqual(providers['dot'], {'ready': True, 'detail': ''})
        self.assertEqual(providers['offline']['ready'], False)
        self.assertEqual(providers['fixture']['detail'], 'Synthetic')

    def test_dialogue_does_not_render_internal_tool_payloads(self):
        self.app.config['coucou_capability'] = 'synthetic-hub'
        self.app.store.switch('coucou', self.context, self.app.store.focus('coucou')['revision'])
        self.app.store.append(self.context, 'user', 'Question', 'fixture')
        self.app.store.append(self.context, 'tool', '{"internal":"fixture"}', 'fixture')
        self.app.store.append(self.context, 'assistant', 'Answer', 'fixture')
        self.assertEqual([e['body'] for e in self.scoped()['history']], ['Question', 'Answer'])
