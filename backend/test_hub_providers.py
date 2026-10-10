"""API adapter protocol fixtures; these do not call paid cloud APIs."""
import copy
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from hub_providers import CloudProvider
from hub_runtime import Coordinator, RuntimeStore


class CloudProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=RuntimeStore(self.tmp.name+'/state')
        context=self.store.create_context('Проверка')['id']
        self.store.put_memory('Формат','Кратко','fixture',0,context)
        self.row,_=self.store.create_task(context,'fixture','cloud','Прочти память',__import__('uuid').uuid4().__str__())
        self.coordinator=Coordinator(self.store,{})
        self.environment=patch.dict(os.environ,{'HUB_TEST_KEY':'fixture-secret'});self.environment.start()

    def tearDown(self):self.environment.stop();self.tmp.cleanup()

    def run_protocol(self,mode,replies):
        captured=[]
        def response(url,payload,headers,timeout):
            captured.append((url,copy.deepcopy(payload),headers))
            return replies[len(captured)-1]
        provider=CloudProvider({'protocol':mode,'url':'https://example.invalid/v1','model':'fixture-model','key_env':'HUB_TEST_KEY'})
        with patch('hub_providers.request_json',side_effect=response):
            result=provider.run(self.row,self.coordinator,threading.Event(),lambda _:None)
        self.assertEqual(result,'Память прочитана.')
        history=self.store.history(self.row['context_id'])
        self.assertTrue(any(e['role']=='tool' and 'memory_read' in e['body'] for e in history))
        return captured

    def test_responses_function_call_roundtrip(self):
        calls=self.run_protocol('openai',[
            {'status':'completed','output':[{'type':'function_call','name':'memory_read','call_id':'call1','arguments':'{"query":""}'}]},
            {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'Память прочитана.'}]}]}])
        self.assertTrue(calls[0][0].endswith('/responses'))
        self.assertFalse(calls[0][1]['store'])
        self.assertEqual(calls[1][1]['input'][-1]['type'],'function_call_output')
        self.assertEqual(calls[1][1]['input'][-1]['call_id'],'call1')

    def test_anthropic_tool_use_roundtrip(self):
        calls=self.run_protocol('anthropic',[
            {'stop_reason':'tool_use','content':[{'type':'tool_use','name':'memory_read','id':'call1','input':{'query':''}}]},
            {'stop_reason':'end_turn','content':[{'type':'text','text':'Память прочитана.'}]}])
        self.assertIn('x-api-key',calls[0][2])
        block=calls[1][1]['messages'][-1]['content'][0]
        self.assertEqual(block['type'],'tool_result');self.assertEqual(block['tool_use_id'],'call1')

    def test_compatible_tool_calls_with_null_content(self):
        calls=self.run_protocol('openai-compatible',[
            {'choices':[{'finish_reason':'tool_calls','message':{'role':'assistant','content':None,'tool_calls':[{'id':'call1','type':'function','function':{'name':'memory_read','arguments':'{"query":""}'}}]}}]},
            {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'Память прочитана.'}}]}])
        self.assertEqual(calls[1][1]['messages'][-1]['role'],'tool')
        self.assertEqual(calls[1][1]['messages'][-1]['tool_call_id'],'call1')

    def test_incomplete_response_is_not_success(self):
        provider=CloudProvider({'protocol':'openai','url':'https://example.invalid/v1','model':'fixture','key_env':'HUB_TEST_KEY'})
        with patch('hub_providers.request_json',return_value={'status':'incomplete','output':[]}):
            with self.assertRaises(RuntimeError):provider.run(self.row,self.coordinator,threading.Event(),lambda _:None)


if __name__=='__main__':unittest.main()
