import json
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_hub import HubStore
from codex_context_bridge import BridgeError, CodexReader, SOURCES, main


FAKE = r'''
import json,sys,time
log=open(sys.argv[1],'w',buffering=1)
mode=sys.argv[2]
initialized=False
for line in sys.stdin:
    msg=json.loads(line); log.write(json.dumps(msg)+'\n')
    if 'method' not in msg: continue
    method=msg['method']
    if method=='initialized': initialized=True; continue
    if method!='initialize' and not initialized: sys.exit(9)
    if method=='initialize': result={'userAgent':'fake'}
    elif method=='thread/list':
        if mode=='timeout': time.sleep(5); continue
        if mode=='oversized':
            print(json.dumps({'id':msg['id'],'result':{'data':['x'*5000]}}),flush=True); continue
        print(json.dumps({'method':'thread/status/changed','params':{}}),flush=True)
        if mode=='approval':
            print(json.dumps({'method':'item/commandExecution/requestApproval','id':900,'params':{}}),flush=True)
        result={'data':[{'id':'native-1','name':'Пример','cwd':'/tmp/project','modelProvider':'openai'}],'nextCursor':'next'}
    elif method=='thread/read': result={'thread':{'id':msg['params']['threadId'],'name':'Пример','cwd':'/tmp/project'}}
    elif method=='thread/turns/list':
        result={'data':[
          {'id':'turn-2','status':'completed','items':[
             {'id':'i2','type':'userMessage','content':[{'type':'text','text':'Что играет?'}]},
             {'id':'i3','type':'mcpToolCall','server':'media','tool':'now_playing','result':{'title':'Track'}},
             {'id':'i4','type':'agentMessage','text':'Играет Track'},
             {'id':'i5','type':'commandExecution','aggregatedOutput':'я'*10000}]},
          {'id':'turn-1','status':'completed','items':[{'id':'i1','type':'agentMessage','text':'Привет'}]}],
          'nextCursor':'older'}
    else: sys.exit(10)
    print(json.dumps({'id':msg['id'],'result':result}),flush=True)
'''


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'requests.jsonl'

    def reader(self,mode='normal',timeout=2):
        return CodexReader(command=[sys.executable,'-u','-c',FAKE,str(self.log),mode],timeout=timeout)

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_handshake_pagination_and_all_native_sources(self):
        with self.reader() as reader:
            page = reader.list_chats(10,'cursor')
            self.assertEqual(page['data'][0]['name'],'Пример')
            self.assertEqual(page['nextCursor'],'next')
        messages=self.requests()
        self.assertEqual([m['method'] for m in messages],['initialize','initialized','thread/list'])
        self.assertEqual(messages[-1]['params']['sourceKinds'],SOURCES)
        self.assertTrue(messages[-1]['params']['useStateDbOnly'])
        self.assertEqual(messages[-1]['params']['cursor'],'cursor')
        self.assertNotIn('jsonrpc',messages[0])

    def test_execution_requests_receive_error_not_approval(self):
        with self.reader('approval') as reader:
            reader.list_chats(); reader.list_chats()
        errors=[m for m in self.requests() if m.get('id')==900]
        self.assertGreaterEqual(len(errors),1)
        self.assertEqual(errors[0]['error']['code'],-32601)
        self.assertNotIn('result',errors[0])

    def test_context_export_preserves_tools_and_explicit_omissions(self):
        with self.reader() as reader:
            packet=reader.export('native-1',8,2048,'page-2')
        self.assertEqual([t['id'] for t in packet['turns']],['turn-1','turn-2'])
        self.assertEqual(packet['olderCursor'],'older')
        self.assertEqual(packet['omittedItems'],1)
        self.assertLessEqual(len(json.dumps(packet,ensure_ascii=False,separators=(',',':')).encode()),2048)
        self.assertIn('mcpToolCall',[i['type'] for i in packet['turns'][1]['items']])
        messages=self.requests()
        self.assertFalse(messages[2]['params']['includeTurns'])
        self.assertEqual(messages[3]['params']['itemsView'],'full')
        self.assertEqual(messages[3]['params']['cursor'],'page-2')

    def test_mutating_methods_are_rejected_before_sending(self):
        with self.reader() as reader:
            for method in ['turn/start','thread/resume','thread/fork','account/login/start']:
                with self.assertRaises(ValueError): reader.call(method,{})
        self.assertEqual(len(self.requests()),2)

    def test_console_import_keeps_source_and_deduplicates_same_page(self):
        state = Path(self.tmp.name) / 'hub'
        store = HubStore(state)
        context = store.create_context('Музыка')['id']
        ids = []
        for _ in range(2):
            output = io.StringIO()
            with patch('codex_context_bridge.CodexReader',side_effect=lambda *_: self.reader()), contextlib.redirect_stdout(output):
                code = main(['--state',str(state),'--host','workstation','export','native-1','--context',context,'--budget','2048'])
            self.assertEqual(code,0)
            ids.append(json.loads(output.getvalue())['entryId'])
        self.assertEqual(ids[0],ids[1])
        packet = store.packet(context)
        self.assertEqual(len(packet['entries']),1)
        self.assertEqual(packet['entries'][0]['role'],'note')
        self.assertEqual(packet['chats'][0]['native_id'],'native-1')
        imported = json.loads(packet['entries'][0]['body'])
        self.assertEqual(imported['contentKind'],'native-chat-data')
        self.assertEqual(imported['omittedItems'],1)

    def test_timeout_is_not_retried(self):
        with self.reader('timeout',0.2) as reader:
            with self.assertRaisesRegex(BridgeError,'timed out'): reader.list_chats()
        self.assertEqual(sum(m.get('method')=='thread/list' for m in self.requests()),1)

    def test_response_bound_stops_oversized_frame(self):
        with self.reader('oversized') as reader, patch('codex_context_bridge.MAX_FRAME',4096):
            with self.assertRaisesRegex(BridgeError,'too large'): reader.list_chats()


if __name__ == '__main__': unittest.main()
