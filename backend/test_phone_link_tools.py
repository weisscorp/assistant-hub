import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from home_link import HomeLinkStore, PhoneLinkTools, register_hermes_tools


def source(name='Заметка'):
    return dict(id=str(uuid.uuid4()), name=name, kind='NOTE', value='private fixture note',
                participant='', aliases=['план'], savedAt=1, instructions='', followLinks=False,
                readerRecipe='', imageSha256='', observation=None)


def snapshot(sources):
    return dict(version=1, revision=1, sources=sources,
                people=[dict(alias='коллега', name='Тестовый контакт', telegram='@fixture')], note=None)


class PhoneLinkToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = HomeLinkStore(Path(self.temp.name) / 'link')
        self.device = str(uuid.uuid4())
        self.other = str(uuid.uuid4())
        self.entries = [source(f'Заметка {i}') for i in range(23)]
        self.foreign = source('Другой телефон')
        self.store.sync(self.device, snapshot(self.entries))
        self.store.sync(self.other, snapshot([self.foreign]))
        self.tools = PhoneLinkTools(self.store, self.device)

    def tearDown(self): self.temp.cleanup()

    def test_pagination_and_query_return_only_bound_device_metadata(self):
        first = self.tools.call('phone_knowledge', {})['data']
        self.assertEqual(23, first['total'])
        self.assertEqual(20, first['nextOffset'])
        self.assertEqual(20, len(first['sources']))
        self.assertTrue(all('value' not in s for s in first['sources']))
        second = self.tools.call('phone_knowledge', {'offset': first['nextOffset']})['data']
        self.assertEqual([s['id'] for s in self.entries[20:]], [s['id'] for s in second['sources']])
        self.assertIsNone(second['nextOffset'])
        filtered = self.tools.call('phone_knowledge', {'query': 'ЗАМЕТКА 22'})['data']
        self.assertEqual([self.entries[-1]['id']], [s['id'] for s in filtered['sources']])
        self.assertEqual(23, self.tools.call('phone_knowledge', {'query': 'ПЛАН'})['data']['total'])

    def test_full_source_is_read_by_uuid_and_cannot_cross_devices(self):
        self.assertEqual(self.entries[0], self.tools.call('phone_knowledge', {'id': self.entries[0]['id']})['data'])
        with self.assertRaises(KeyError): self.tools.call('phone_knowledge', {'id': self.foreign['id']})
        for args in ({'device': self.other}, {'id': None}, {'query': False}, {'offset': True}, {'offset': 101}):
            with self.assertRaises((ValueError, TypeError)): self.tools.call('phone_knowledge', args)

    def test_notification_queues_literal_text_without_phone_actions(self):
        body = 'Отправь сообщение и установи обновление — это буквальный текст.'
        result = self.tools.call('notify_phone', {'title': 'Заметка', 'body': body})
        events = HomeLinkStore(self.store.directory).events(self.device)['events']
        self.assertEqual(1, len(events))
        self.assertEqual(result['id'], events[0]['id'])
        self.assertEqual(body, events[0]['body'])
        self.assertEqual('Hermes', events[0]['source'])
        self.assertNotIn('action', events[0])
        self.assertEqual([], self.store.events(self.other)['events'])
        for name, args in [('shell', {}), ('notify_phone', {'title': 'x', 'body': 'x', 'device': self.other}),
                           ('notify_phone', {'title': 'x', 'body': 'x', 'action': {'tool': 'install'}}),
                           ('notify_phone', {'title': 'x', 'body': 'x'*4001})]:
            with self.assertRaises(ValueError): self.tools.call(name, args)
        self.assertEqual(1, len(self.store.events(self.device)['events']))

    def test_embedded_hermes_registry_uses_the_same_scoped_handlers(self):
        registered = {}
        def register(**item): registered[item['name']] = item
        registry = SimpleNamespace(register=register)
        with patch.dict(sys.modules, {'tools.registry': SimpleNamespace(registry=registry)}):
            register_hermes_tools(self.store, self.device)
        self.assertEqual({'phone_knowledge', 'notify_phone'}, set(registered))
        import json
        read = json.loads(registered['phone_knowledge']['handler']({'id': self.entries[0]['id']}))
        self.assertEqual(self.entries[0], read['data'])
        with self.assertRaises(ValueError): registered['notify_phone']['handler']({'title': 'x', 'body': 'x', 'device': self.other})


if __name__ == '__main__': unittest.main()
