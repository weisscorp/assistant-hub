"""Regression: HTTP LAN pages have getRandomValues but not randomUUID."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from hub_incidents import ModelEvaluator


class BrowserAndDeadlineTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed for browser UUID regression')
    def test_http_browser_ids_are_valid_unique_uuid4_without_random_uuid_api(self):
        for name in ('app.js', 'awareness.js'):
            prefix = (Path(__file__).parent / 'hub_ui' / name).read_text().split('const $', 1)[0]
            program = '''const vm=require('node:vm'), crypto=require('node:crypto').webcrypto;
const context={crypto:{getRandomValues:array=>crypto.getRandomValues(array)}};
vm.createContext(context);vm.runInContext(SOURCE,context);
const ids=Array.from({length:1000},()=>context.requestId());
if(new Set(ids).size!==1000 || ids.some(id=>!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id)))process.exit(1);
'''.replace('SOURCE', json.dumps(prefix))
            subprocess.run(['node', '-e', program], check=True, capture_output=True)

    def test_assessment_deadline_is_configurable_and_bounded(self):
        provider = {'type': 'ollama', 'model': 'fixture', 'url': 'http://127.0.0.1:1'}
        row = {'source': 'fixture', 'title': 'fixture', 'content_type': 'application/json', 'payload': '{}'}
        report = {'summary': 'fixture', 'severity': 'info', 'confidence': 'high', 'suggestions': []}
        with patch('hub_incidents.request_json', return_value={'done': True, 'message': {'content': json.dumps(report)}}) as call:
            self.assertEqual(ModelEvaluator(provider, timeout=300).evaluate(row), report)
            self.assertEqual(call.call_args.kwargs['timeout'], 300)
        for timeout in (True, 0, 601, '300'):
            with self.assertRaises(ValueError):
                ModelEvaluator(provider, timeout=timeout)


if __name__ == '__main__':
    unittest.main()
