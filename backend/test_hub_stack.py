"""Coordinator/API integration tests: actual HTTP and SQLite, deterministic provider."""
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from agent_hub import Conflict
from hub_runtime import Coordinator, RuntimeStore, TERMINAL
from hub_server import HubApplication, HubHandler


class Fixture:
    def __init__(self):self.calls=0;self.gate=threading.Event();self.gate.set()
    def status(self):return {'ready':True,'detail':'Fixture'}
    def run(self,row,coordinator,stop,progress):
        self.calls+=1
        while not self.gate.wait(.01):
            if stop.is_set():raise InterruptedError()
        result=coordinator.call_tool('memory_read',{'query':''},row,stop)
        reply='Факты: '+','.join(m['key'] for m in result)
        progress(reply);return reply


class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.token=secrets.token_urlsafe(48);(root/'token').write_text(self.token);(root/'token').chmod(0o600)
        config={'state':str(root/'state'),'token_file':str(root/'token'),'providers':{}}
        self.app=HubApplication(config);self.fixture=Fixture()
        self.app.providers['fixture']=self.fixture
        self.server=ThreadingHTTPServer(('127.0.0.1',0),HubHandler);self.server.app=self.app
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.context=self.app.store.contexts()[0]['id']
        self.other=self.app.store.create_context('Другая тема')['id']

    def tearDown(self):
        self.fixture.gate.set()
        for row in self.app.store.tasks():
            if row['status'] not in TERMINAL:self.app.coordinator.cancel(row['id'])
        deadline=time.monotonic()+2
        while self.app.coordinator.active and time.monotonic()<deadline:time.sleep(.01)
        self.server.shutdown();self.server.server_close();self.tmp.cleanup()

    def request(self,path,body=None,auth=True,headers=None):
        h={'Content-Type':'application/json',**({'Authorization':'Bearer '+self.token} if auth else {}),**(headers or {})}
        r=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=h)
        with urllib.request.urlopen(r,timeout=5) as response:return json.load(response)

    def submit(self,id=None,prompt='Прочти память'):
        return self.request('/api/messages',{'id':id or str(uuid.uuid4()),'context_id':self.context,'client':'test','provider':'fixture','prompt':prompt})

    def wait(self,id):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            t=self.request('/api/tasks/'+id)
            if t['status'] in TERMINAL:return t
            time.sleep(.01)
        self.fail('task timeout')

    def test_auth_origin_and_single_use_ticket(self):
        for path,headers in [('/api/state',{}),('/api/state',{'Origin':'http://evil.invalid'}),('/api/state',{'Host':'evil.invalid'})]:
            with self.assertRaises(urllib.error.HTTPError):self.request(path,auth=bool(headers),headers=headers)
        ticket=self.request('/api/tickets',{})['path']
        with urllib.request.urlopen(self.url+ticket) as response:
            self.assertEqual(response.status,200)
        with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(self.url+ticket)

    def test_http_idempotent_concurrent_submit_and_conflict(self):
        id=str(uuid.uuid4())
        with ThreadPoolExecutor(6) as pool:rows=list(pool.map(lambda _:self.submit(id),range(6)))
        self.assertEqual({r['id'] for r in rows},{id});self.assertEqual(self.wait(id)['status'],'completed')
        self.assertEqual(self.fixture.calls,1)
        self.assertEqual(len([e for e in self.app.store.history(self.context) if e['role']=='user']),1)
        with self.assertRaises(urllib.error.HTTPError) as e:self.submit(id,'Другой запрос')
        self.assertEqual(e.exception.code,409)

    def test_tools_scope_events_and_handoff(self):
        s=self.app.store;s.put_memory('Общее','Факт','test',0);s.put_memory('Тема','Факт','test',0,self.context);s.put_memory('Скрыто','Секрет','test',0,self.other)
        row=self.wait(self.submit()['id']);self.assertEqual(set(row['output'].removeprefix('Факты: ').split(',')),{'Общее','Тема'})
        # Terminal status is persisted before the worker's final session event.
        # Wait for its lifecycle to end before asserting no newer events exist.
        deadline=time.monotonic()+3
        while row['id'] in self.app.coordinator.active and time.monotonic()<deadline:time.sleep(.01)
        self.assertNotIn(row['id'],self.app.coordinator.active)
        events=self.request('/api/events?after=0')
        self.assertTrue(any(e['kind']=='tool.succeeded' for e in events))
        self.assertTrue(any(e['kind']=='task.completed' for e in events))
        self.assertFalse(self.request('/api/events?after='+str(events[-1]['seq'])))
        notification=s.notify(self.other,'test','Обсудим другую тему')
        self.assertEqual(s.focus('test')['context_id'],None)
        focus=self.request('/api/follow',{'client':'test','notification':notification['id'],'revision':0})
        self.assertEqual(focus['context_id'],self.other)

    def test_mcp_parent_task_indexes_source_phrase_and_checks_scope(self):
        row,_=self.app.store.create_task(self.context,'test','hermes','Мой проверочный код — сосна-21.',str(uuid.uuid4()))
        body={'name':'memory_put','arguments':{'key':'code','body':'сосна-21','global':True,'revision':0},
              'context_id':self.context,'client':'hermes','request_id':str(uuid.uuid4()),'task_id':row['id']}
        self.request('/api/tool',body)
        self.assertEqual(self.app.store.search_memory('проверочный код',self.context)[0]['body'],'сосна-21')
        body['context_id']=self.other
        with self.assertRaises(urllib.error.HTTPError) as e:self.request('/api/tool',body)
        self.assertEqual(e.exception.code,400)

    def test_context_api_can_inspect_indexed_selection(self):
        self.app.store.put_memory('route','Навигатор до аэропорта','test',0)
        self.app.store.put_memory('unrelated','Метеорит','test',0)
        path='/api/context?id='+self.context+'&query='+__import__('urllib.parse',fromlist=['quote']).quote('навигатор')
        selected=self.request(path)
        self.assertEqual([m['key'] for m in selected['memory']],['route'])
        self.assertEqual(selected['retrieval']['selected'],1)

    def test_state_preview_is_bounded_and_knowledge_api_is_paginated(self):
        for i in range(35):self.app.store.put_memory('fact-'+str(i),'Знание '+str(i),'test',0)
        with __import__('unittest.mock',fromlist=['patch']).patch.object(self.app.store,'memories',side_effect=AssertionError('full list forbidden')):
            preview=self.request('/api/state?client=test')
            self.assertEqual(len(preview['memory']),12)
            self.assertTrue(preview['memoryHasMore'])
            first=self.request('/api/memory?context='+self.context+'&limit=20')
            self.assertEqual(len(first['items']),20)
            scope,key=first['next']
            second=self.request('/api/memory?context='+self.context+'&limit=20&after_scope='+scope+'&after_key='+key)
            self.assertEqual(len(second['items']),15)
            self.assertIsNone(second['next'])
            self.assertEqual(len({m['key'] for m in first['items']+second['items']}),35)

    def test_cancel_discards_late_reply_and_blocks_queued_turn(self):
        self.fixture.gate.clear();one=self.submit();two=self.submit()
        cancelled=self.request('/api/cancel',{'id':one['id']});self.assertEqual(cancelled['status'],'cancelled')
        self.request('/api/cancel',{'id':two['id']});self.fixture.gate.set()
        for _ in range(100):
            if not self.app.coordinator.active:break
            time.sleep(.01)
        self.assertEqual(self.app.store.task(one['id'])['status'],'cancelled')
        self.assertEqual(self.app.store.task(two['id'])['status'],'cancelled')
        self.assertFalse(any(e['body'].startswith('Факты:') for e in self.app.store.history(self.context)))

    def test_restart_marks_uncertain_and_never_replays(self):
        row,_=self.app.store.create_task(self.context,'test','fixture','Запрос',str(uuid.uuid4()))
        RuntimeStore(self.app.store.directory).recover()
        self.assertEqual(self.app.store.task(row['id'])['status'],'unknown');self.assertEqual(self.fixture.calls,0)

    def test_openai_alias_topic_binding_stream_and_error_answer(self):
        model='hub/fixture/'+self.context
        self.assertIn(model,[m['id'] for m in self.request('/v1/models')['data']])
        body={'model':model,'messages':[{'role':'user','content':'Привет'}]}
        result=self.request('/v1/chat/completions',body)
        self.assertEqual(result['choices'][0]['message']['content'],'Факты:')
        body['stream']=True
        r=urllib.request.Request(self.url+'/v1/chat/completions',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(r,timeout=5) as response:wire=response.read().decode()
        self.assertIn('data: [DONE]',wire);self.assertIn('Факты:',wire)
        self.assertEqual(self.fixture.calls,2)

    def test_phone_contract_knowledge_sync_and_scoped_coucou_access(self):
        device=str(uuid.uuid4());source=str(uuid.uuid4())
        snapshot={'version':1,'revision':1,'sources':[{'id':source,'name':'Тестовый источник','kind':'NOTE','value':'Проверочный факт',
            'participant':'','aliases':[],'savedAt':1,'instructions':'','followLinks':False,'readerRecipe':'','imageSha256':'','observation':None}],
            'people':[],'note':None}
        self.request('/link/'+device+'/knowledge',snapshot)
        self.assertEqual(self.request('/link/'+device+'/knowledge')['revision'],1)
        result=self.request('/api/tool',{'name':'phone_knowledge','arguments':{'device':device,'source':source},
            'context_id':self.context,'client':'test','request_id':str(uuid.uuid4())})
        self.assertEqual(result['value'],'Проверочный факт')
        self.app.config['mobile_provider']='fixture'
        voice=self.request('/conversation',{'version':1,'session':str(uuid.uuid4()),'phoneDevice':device,'id':str(uuid.uuid4()),
            'text':'Вопрос','history':[],'capabilities':[]})
        self.assertEqual(voice['version'],1);self.assertTrue(voice['reply']);self.assertIsNone(voice['action'])
        events=self.request('/link/'+device+'/events?after=0&wait=0')
        self.assertTrue(events['events'])
        self.app.config['coucou_capability']='test-capability'
        models=self.request('/coucou/test-capability/v1/models',auth=False,headers={'Authorization':'Bearer ollama'})
        self.assertTrue(models['data'])
        with self.assertRaises(urllib.error.HTTPError):self.request('/api/state',auth=False,headers={'Authorization':'Bearer ollama'})


if __name__=='__main__':unittest.main()
