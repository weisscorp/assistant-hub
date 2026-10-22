"""Routing protocol/coordination fixtures; no actual model or paid agent calls.

The mocked decisions test protocol and topic isolation, not model accuracy.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import importlib.util
import io
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import sys
import unittest
from unittest.mock import patch
import uuid

from agent_hub import Conflict
from hub_providers import OllamaProvider
from hub_provider_errors import ProviderFailure
from hub_routing import RoutingProvider, ROUTE_SCHEMA
from hub_runtime import Coordinator, RuntimeStore, TERMINAL


def decision(route):
    return {'done': True, 'message': {'role': 'assistant', 'content': json.dumps({'route': route})}}


class Fixture:
    def __init__(self, ready=True):
        self.ready, self.calls = ready, []

    def status(self):
        return {'ready': self.ready, 'detail': 'Synthetic fixture'}

    def run(self, row, coordinator, stop, progress):
        self.calls.append(dict(row))
        return 'Ответ тестового агента.'


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(self.tmp.name + '/state')
        self.context = self.store.create_context('Тестовая тема')['id']
        self.general, self.coding = Fixture(), Fixture()
        self.classifier = OllamaProvider('http://127.0.0.1:9', 'fixture-classifier')
        self.providers = {'general': self.general, 'coding': self.coding, 'classifier': self.classifier}
        self.router = RoutingProvider({'general': 'general', 'coding': 'coding', 'classifier': 'classifier'}, self.providers)
        self.providers['auto'] = self.router
        self.coordinator = Coordinator(self.store, self.providers)

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, prompt, context=None):
        return self.store.create_task(context or self.context, 'fixture', 'auto', prompt, str(uuid.uuid4()))[0]

    def test_schema_and_coding_delegation_preserve_request_and_logical_provider(self):
        row = self.row('Исправь функцию parse_date и добавь тест на часовой пояс.')
        with patch('hub_routing.request_json', return_value=decision('coding')) as call:
            result = self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
        self.assertEqual(result, 'Ответ тестового агента.')
        self.assertEqual(self.coding.calls[0]['provider'], 'coding')
        self.assertEqual(self.coding.calls[0]['prompt'], row['prompt'])
        self.assertEqual(self.store.task(row['id'])['provider'], 'auto')
        self.assertFalse(self.general.calls)
        body = call.call_args.args[1]
        self.assertEqual(body['format'], ROUTE_SCHEMA)
        self.assertEqual(body['options']['temperature'], 0)
        self.assertFalse(body['stream'])
        self.assertNotIn('tools', body)
        self.assertEqual(body['messages'][-1], {'role': 'user', 'content': row['prompt']})
        events = [e for e in self.store.events() if e['kind'] == 'task.routed']
        self.assertEqual(events[0]['data']['target'], 'coding')

    def test_continuation_has_completed_topic_history_and_no_other_topic_or_queued_input(self):
        first = self.row('Напиши скрипт для проверки JSONL.')
        self.store.update(first['id'], 'completed', output='Скрипт подготовлен.')
        unrelated = self.store.create_context('Посторонняя тема')['id']
        self.store.append(unrelated, 'user', 'Закажи билеты.', 'fixture')
        row = self.row('Продолжай, теперь добавь обработку пустой строки.')
        self.row('Расскажи о погоде.')
        with patch('hub_routing.request_json', return_value=decision('coding')) as call:
            self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
        history = call.call_args.args[1]['messages'][1:]
        self.assertEqual(history, [
            {'role': 'user', 'content': first['prompt']},
            {'role': 'assistant', 'content': 'Скрипт подготовлен.'},
            {'role': 'user', 'content': row['prompt']}])
        self.assertEqual(len(self.coding.calls), 1)

    def test_no_history_is_explicit_and_ambiguous_continuation_is_not_inferred_in_code(self):
        for prompt in ('Продолжай.', 'Поправь.', 'Да.'):
            with self.subTest(prompt=prompt):
                row = self.row(prompt)
                with patch('hub_routing.request_json', return_value=decision('general')) as call:
                    self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
                messages = call.call_args.args[1]['messages']
                self.assertEqual(len(messages), 2)
                self.assertIn('Доступная история текущей темы ПУСТА.', messages[0]['content'])
                self.assertEqual(messages[-1]['content'], prompt)
        self.assertEqual(len(self.general.calls), 3)
        self.assertFalse(self.coding.calls)

    def test_topic_switch_negation_and_dictation_follow_model_decision(self):
        earlier = self.row('Исправь тесты в проекте.')
        self.store.update(earlier['id'], 'completed', output='Тесты исправлены в учебном примере.')
        prompts = ['Теперь другая тема: поставь таймер на две минуты.',
                   'Не меняй код, просто объясни, что такое рекурсия.',
                   'Отправь сообщение: исправь баг завтра.']
        for prompt in prompts:
            row = self.row(prompt)
            with patch('hub_routing.request_json', return_value=decision('general')) as call:
                self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
            self.assertEqual(call.call_args.args[1]['messages'][-1]['content'], prompt)
        self.assertEqual([r['prompt'] for r in self.general.calls], prompts)
        self.assertFalse(self.coding.calls)

    def test_long_code_result_does_not_erase_user_task_for_continuation(self):
        previous = self.row('Добавь обработку JSONL в скрипт.')
        self.store.update(previous['id'], 'completed', output='x' * 19000)
        row = self.row('Продолжай.')
        with patch('hub_routing.request_json', return_value=decision('coding')) as call:
            self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
        messages = call.call_args.args[1]['messages']
        self.assertEqual(messages[1]['content'], previous['prompt'])
        self.assertLess(len(messages[2]['content']), 4000)
        self.assertEqual(messages[-1]['content'], 'Продолжай.')

    def test_persisted_assignment_does_not_reclassify_or_change_target(self):
        row = self.row('Напиши функцию проверки имени.')
        with patch('hub_routing.request_json', return_value=decision('coding')) as call:
            self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
            self.router.routes['coding'] = 'general'
            self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(len(self.coding.calls), 2)
        self.assertFalse(self.general.calls)
        self.assertEqual(len([e for e in self.store.events() if e['kind'] == 'task.routed']), 1)

    def test_invalid_or_failed_classifier_never_executes_a_provider(self):
        bad = [None, {'done': False}, {'done': True, 'error': 'fixture-error'},
               {**decision('general'), 'done_reason': 'length'},
               {'done': True, 'message': {'content': '{"route":"unknown"}'}},
               {'done': True, 'message': {'content': '{"route":"coding","execute":true}'}},
               {'done': True, 'message': {'content': 'coding'}},
               {'done': True, 'message': {'content': '{"route":"coding"}', 'tool_calls': [{}]}}]
        for response in bad:
            row = self.row('Сделай новый тест.')
            with self.subTest(response=response), patch('hub_routing.request_json', return_value=response):
                with self.assertRaises(ProviderFailure):
                    self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
            self.assertIsNone(self.store.setting('routing:task:' + row['id']))
        row = self.row('Сделай новый тест.')
        with patch('hub_routing.request_json', side_effect=OSError('private upstream text')):
            with self.assertRaises(ProviderFailure) as error:
                self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
        self.assertNotIn('private upstream text', str(error.exception))
        self.assertFalse(self.general.calls)
        self.assertFalse(self.coding.calls)

    def test_unavailable_coding_does_not_fall_back_but_auto_supports_general(self):
        self.coding.ready = False
        with patch.object(self.classifier, 'status', return_value={'ready': True}):
            status = self.router.status()
        self.assertTrue(status['ready'])
        self.assertFalse(status['routes']['coding']['ready'])
        row = self.row('Исправь ошибку компиляции.')
        with patch('hub_routing.request_json', return_value=decision('coding')):
            with self.assertRaises(ProviderFailure):
                self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
        self.assertFalse(self.general.calls)
        self.assertFalse(self.coding.calls)
        row = self.row('Привет.')
        with patch('hub_routing.request_json', return_value=decision('general')):
            self.router.run(row, self.coordinator, threading.Event(), lambda _: None)
        self.assertEqual(len(self.general.calls), 1)

    def test_deferred_classifier_wrong_type_and_missing_config_are_rejected(self):
        self.coding.deferred = True
        with self.assertRaises(ValueError):
            RoutingProvider(self.router.routes, self.providers)
        del self.coding.deferred
        with self.assertRaises(ValueError):
            RoutingProvider({**self.router.routes, 'classifier': 'general'}, self.providers)
        with self.assertRaises(ValueError):
            RoutingProvider({**self.router.routes, 'coding': 'absent'}, self.providers)

    def test_cancelled_before_classification_never_calls_model_or_agent(self):
        stop = threading.Event(); stop.set()
        with patch('hub_routing.request_json') as call:
            with self.assertRaises(InterruptedError):
                self.router.run(self.row('Продолжай.'), self.coordinator, stop, lambda _: None)
        call.assert_not_called()
        self.assertFalse(self.coding.calls)


class HubRoutingIntegrationTests(unittest.TestCase):
    def setUp(self):
        from hub_server import HubApplication
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'token').write_text(secrets.token_urlsafe(48))
        (root / 'token').chmod(0o600)
        self.coding = Fixture()
        # The router deliberately appears before its targets in configuration.
        config = {'state': str(root / 'state'), 'token_file': str(root / 'token'), 'providers': {
            'auto': {'type': 'routing', 'general': 'ollama', 'coding': 'mausbot', 'classifier': 'ollama'},
            'mausbot': {'type': 'mausbot', 'url': 'http://127.0.0.1:9', 'bot_id': 'synthetic-bot'},
            'ollama': {'type': 'ollama', 'url': 'http://127.0.0.1:9', 'model': 'fixture-model'}}}
        with patch('hub_server.MausBotProvider', return_value=self.coding):
            self.app = HubApplication(config)
        self.context = self.app.store.contexts()[0]['id']
        self.status = patch.object(self.app.providers['ollama'], 'status', return_value={'ready': True})
        self.status.start()
        self.general = patch.object(self.app.providers['ollama'], 'run', return_value='Обычный ответ.')
        self.general.start()

    def tearDown(self):
        for row in self.app.store.tasks():
            if row['status'] not in TERMINAL:
                self.app.coordinator.cancel(row['id'])
        deadline = time.monotonic() + 2
        while self.app.coordinator.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.general.stop(); self.status.stop(); self.tmp.cleanup()

    def wait(self, task_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            row = self.app.store.task(task_id)
            if row['status'] in TERMINAL:
                return row
            time.sleep(.01)
        self.fail('task timed out')

    def test_uuid_retry_is_one_route_one_coding_execution_and_manual_provider_bypasses_router(self):
        task_id = str(uuid.uuid4())
        with patch('hub_routing.request_json', return_value=decision('coding')) as classify:
            with ThreadPoolExecutor(5) as executor:
                rows = list(executor.map(lambda _: self.app.coordinator.submit(
                    self.context, 'fixture', 'auto', 'Добавь тест для parser.', task_id), range(5)))
            final = self.wait(task_id)
            self.assertEqual({row['id'] for row in rows}, {task_id})
            self.assertEqual(final['status'], 'completed')
            self.assertEqual(final['provider'], 'auto')
            self.assertEqual(len(self.coding.calls), 1)
            self.assertEqual(self.coding.calls[0]['provider'], 'mausbot')
            self.assertEqual(classify.call_count, 1)
            retry = self.app.coordinator.submit(self.context, 'fixture', 'auto', 'Добавь тест для parser.', task_id)
            self.assertEqual(retry['status'], 'completed')
            with self.assertRaises(Conflict):
                self.app.coordinator.submit(self.context, 'fixture', 'mausbot', 'Добавь тест для parser.', task_id)
            manual = self.app.coordinator.submit(self.context, 'fixture', 'mausbot', 'Напиши fixture.', str(uuid.uuid4()))
            self.assertEqual(self.wait(manual['id'])['status'], 'completed')
            self.assertEqual(len(self.coding.calls), 2)
            self.assertEqual(classify.call_count, 1)

    def test_unavailable_coding_is_failed_with_no_general_fallback(self):
        self.coding.ready = False
        with patch('hub_routing.request_json', return_value=decision('coding')):
            task = self.app.coordinator.submit(self.context, 'fixture', 'auto', 'Исправь тест.', str(uuid.uuid4()))
            final = self.wait(task['id'])
        self.assertEqual(final['status'], 'failed')
        self.assertIn('кодинга недоступен', final['error'])
        self.assertFalse(self.coding.calls)
        self.app.providers['ollama'].run.assert_not_called()


class SetupRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent.parent / 'tools' / 'local_stack.py'
        spec = importlib.util.spec_from_file_location('local_stack_routing_fixture', path)
        cls.stack = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.stack)

    def test_setup_preserves_unrelated_private_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = {'host': '127.0.0.1', 'port': 8778, 'state': str(root / 'state'),
                        'token_file': str(root / 'token'), 'providers': {
                            'ollama': {'type': 'ollama', 'url': 'http://127.0.0.1:9', 'model': 'fixture'},
                            'custom': {'type': 'command', 'argv': ['synthetic-command'], 'label': 'Fixture'}}}
            (root / 'config.json').write_text(json.dumps(existing))
            (root / 'config.json').chmod(0o600)
            args = argparse.Namespace(directory=root, local_node=False, hermes_root=None, hermes_python=None,
                                      mausbot_url='http://127.0.0.1:3000', mausbot_bot='synthetic-bot',
                                      mausbot_token_file=root / 'mausbot-token', route_coding=True)
            with patch.object(self.stack.shutil, 'which', return_value=None):
                result = self.stack.configure(args)
            self.assertEqual(result['providers']['custom'], existing['providers']['custom'])
            self.assertEqual(result['providers']['ollama'], existing['providers']['ollama'])
            self.assertEqual(result['providers']['auto']['coding'], 'mausbot')
            self.assertEqual(result['providers']['mausbot']['bot_id'], 'synthetic-bot')
            self.assertEqual(result['providers']['mausbot']['token_file'], str(root / 'mausbot-token'))
            self.assertEqual((root / 'config.json').stat().st_mode & 0o777, 0o600)

    def test_ask_defaults_to_configured_auto_and_preserves_explicit_choice(self):
        scenarios = [({'auto': {}, 'ollama': {}, 'mausbot': {}}, [], 'auto'),
                     ({'auto': {}, 'ollama': {}, 'mausbot': {}}, ['--provider', 'mausbot'], 'mausbot'),
                     ({'ollama': {}}, [], 'ollama')]
        for providers, arguments, expected in scenarios:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                captured = []
                def request(root, path, body=None):
                    if path.startswith('/api/state'):
                        return {'focus': {'context_id': str(uuid.uuid4())}, 'providers': providers}
                    captured.append(body)
                    return {'id': body['id'], 'status': 'completed', 'output': 'Тестовый ответ.'}
                argv = ['local_stack.py', '--directory', directory, 'ask', 'Тестовый запрос.', *arguments]
                with patch.object(sys, 'argv', argv), patch.object(self.stack, 'hub_request', side_effect=request), patch('sys.stdout', new=io.StringIO()):
                    self.assertEqual(self.stack.main(), 0)
                self.assertEqual(captured[0]['provider'], expected)


if __name__ == '__main__':
    unittest.main()
