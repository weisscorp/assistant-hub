"""Real SQLite/HTTP and external owner process; no user accounts or devices."""
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from agent_hub import Conflict, HubStore
from home_link import HomeLinkStore
from hub_awareness import Awareness, demo_events
from hub_context_router import ContextRouter
from hub_server import HubApplication, HubHandler


def key():return str(uuid.uuid4())
def turn(text, session=None):return {'version':1,'session':session or key(),'id':key(),'text':text,'history':[], 'capabilities':[]}


class AwarenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=HubStore(self.root/'hub');self.link=HomeLinkStore(self.root/'phone')
        self.inbox=Awareness(self.store,self.link)
        self.device=key();self.link.sync(self.device,{'version':1,'revision':1,'sources':[],'people':[],'note':None})
    def tearDown(self):self.tmp.cleanup()
    def test_cross_channel_correlation_and_project_boundary(self):
        rows=[self.inbox.accept(e,'fixture') for e in demo_events()]
        self.assertEqual(3,len(rows[2]['events']));self.assertEqual(rows[0]['id'],rows[2]['id'])
        other=self.inbox.accept(dict(demo_events()[0],project='Other'),'fixture-2')
        self.assertNotEqual(rows[0]['id'],other['id'])
    def test_retries_conflicts_and_parallel_ingestion(self):
        event=demo_events()[0]
        with ThreadPoolExecutor(4) as pool:rows=list(pool.map(lambda _:self.inbox.accept(event,'fixture'),range(8)))
        self.assertEqual({1},{r['revision'] for r in rows})
        with self.assertRaises(Conflict):self.inbox.accept(dict(event,body='changed'),'fixture')
        self.assertEqual(1,len(self.inbox.snapshot()['cases']))
    def test_decisions_revision_snooze_and_new_event_reopens(self):
        row=self.inbox.accept(demo_events()[0],'fixture')
        snoozed=self.inbox.decide(row['id'],1,'snooze',60)
        with self.assertRaises(Conflict):self.inbox.decide(row['id'],1,'close')
        self.assertEqual('snoozed',snoozed['effective_status'])
        with self.store.connect() as db:db.execute('UPDATE awareness_cases SET snoozed_until=?',(time.time()-1,))
        self.assertEqual('open',self.inbox.snapshot()['cases'][0]['effective_status'])
        self.inbox.decide(row['id'],2,'close')
        updated=self.inbox.accept(demo_events()[1],'fixture')
        self.assertEqual('open',updated['status']);self.assertEqual(2,len(updated['decisions']))
    def test_phone_publication_idempotent_and_not_read(self):
        row=self.inbox.accept(demo_events()[0],'fixture')
        first=self.inbox.publish(row['id'],1,self.device)
        self.assertEqual(first,self.inbox.publish(row['id'],1,self.device))
        events=self.link.events(self.device)['events']
        self.assertEqual(1,len(events));self.assertEqual('message',events[0]['kind'])
        self.assertEqual('queued_on_phone',first['status'])
        with self.assertRaises(ValueError):self.inbox.publish(row['id'],1,key())
        self.assertEqual(1,len(Awareness(self.store,self.link).snapshot()['cases']))
    def test_input_links_actions_and_types(self):
        for extra in [{'url':'javascript:alert(1)'},{'action':'execute'},{'needs_decision':'yes'},{'priority':'urgent'}]:
            with self.assertRaises(ValueError):self.inbox.accept(dict(demo_events()[0],**extra),'fixture')
    def test_retry_after_phone_write_before_receipt_is_safe(self):
        row=self.inbox.accept(demo_events()[0],'fixture');original=self.link.publish
        def interrupted(*args):original(*args);raise ValueError('lost receipt')
        self.link.publish=interrupted
        with self.assertRaises(Conflict):self.inbox.publish(row['id'],1,self.device)
        self.link.publish=original;self.inbox.publish(row['id'],1,self.device)
        self.assertEqual(1,len(self.link.events(self.device)['events']))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=HubStore(self.root/'hub')
        script=str(Path(__file__).resolve().parents[1]/'tools'/'awareness_demo.py')
        routes=[{'id':name,'label':name,'owner':name,'aliases':[alias],
                 'context_refs':{'product_id':name,'feature_id':name+'-feature'},
                 'argv':[sys.executable,script,'--owner-state',str(self.root/name)]} for name,alias in [('work','работе'),('project','проекте')]]
        self.router=ContextRouter(self.store,{'routes':routes})
    def tearDown(self):self.tmp.cleanup()
    def test_mobile_switches_without_mixed_history_and_owner_keeps_context(self):
        session=key();first=turn('Что на работе?',session);first['history']=[{'user':'SECRET-other-project','assistant':'SECRET'}]
        reply=self.router.turn(first,'phone');self.assertIn('work-feature',reply);self.assertIn('Сообщений в этом контексте: 1',reply)
        self.assertIn('project-feature',self.router.turn(turn('Что в проекте?',session),'phone'))
        follow=self.router.turn(turn('продолжай',session),'phone');self.assertIn('project-feature',follow);self.assertIn('контексте: 2',follow)
        self.assertNotIn('SECRET',follow)
        with self.store.connect() as db:
            self.assertEqual(0,db.execute('SELECT count(*) FROM entries').fetchone()[0])
            self.assertEqual(0,db.execute('SELECT count(*) FROM memory').fetchone()[0])
            self.assertEqual(3,db.execute('SELECT count(*) FROM context_route_requests').fetchone()[0])
    def test_ambiguous_new_question_does_not_inherit_previous_work(self):
        session=key();self.router.turn(turn('Что на работе?',session),'phone')
        for prompt in ['Как дела?','На работе и в проекте что нового?']:
            self.assertIn('К какому контексту',self.router.turn(turn(prompt,session),'phone'))
        self.assertIn('К какому контексту',self.router.turn(turn('продолжай'),'another-phone'))
    def test_retry_is_not_sent_twice_and_failure_is_unknown(self):
        body=turn('Что на работе?');self.router.turn(body,'phone')
        self.assertIn('уже передан',self.router.turn(body,'phone'))
        self.router.routes['work']['argv']=[sys.executable,'-c','raise SystemExit(1)']
        body=turn('Что на работе?');self.assertIn('не подтвердил',self.router.turn(body,'phone'))
        with self.store.connect() as db:self.assertEqual('unknown',db.execute('SELECT status FROM context_route_requests WHERE id=?',(body['id'],)).fetchone()[0])


class AwarenessHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);self.token=secrets.token_urlsafe(48)
        (root/'token').write_text(self.token);(root/'token').chmod(0o600)
        self.app=HubApplication({'state':str(root/'hub'),'token_file':str(root/'token'),'providers':{},'awareness_demo':True})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),HubHandler);self.server.app=self.app
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):self.server.shutdown();self.thread.join();self.server.server_close();self.tmp.cleanup()
    def request(self,path,body=None,token=None):
        request=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,
                                       headers={'Authorization':'Bearer '+(token or self.token),'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=5) as response:return json.load(response)
    def test_http_demo_and_mobile_summary_existing_wire_contract(self):
        self.request('/api/awareness/demo',{})
        snapshot=self.request('/api/awareness');self.assertEqual(2,len(snapshot['cases']))
        request=turn('сводка событий');reply=self.request('/conversation',request)
        self.assertEqual(request['id'],reply['id']);self.assertIsNone(reply['action']);self.assertIn('релиз',reply['reply'].lower())
    def test_webhook_key_can_ingest_but_cannot_read_decide_or_publish(self):
        _,token=self.app.auth.issue('fixture','webhook')
        self.request('/api/awareness/events',demo_events()[0],token)
        for path,body in [('/api/awareness',None),('/api/awareness/decision',{}),('/api/awareness/publish',{}),('/api/awareness/demo',{})]:
            with self.assertRaises(urllib.error.HTTPError) as error:self.request(path,body,token)
            self.assertEqual(403,error.exception.code)
        with self.assertRaises(urllib.error.HTTPError) as error:self.request('/api/awareness',token='wrong')
        self.assertEqual(401,error.exception.code)
    def test_existing_mobile_conversation_uses_owner_without_hub_history(self):
        script=str(Path(__file__).resolve().parents[1]/'tools'/'awareness_demo.py')
        self.app.context_router=ContextRouter(self.app.store,{'routes':[
            {'id':'work','label':'Работа','owner':'work-agent','aliases':['работе'],
             'context_refs':{'feature_id':'feature-fixture'},
             'argv':[sys.executable,script,'--owner-state',str(Path(self.tmp.name)/'owner')]}]})
        body=turn('Что на работе?');body['phoneDevice']=key()
        body['history']=[{'user':'Чужой проект: secret','assistant':'secret'}]
        reply=self.request('/conversation',body)
        self.assertEqual(body['id'],reply['id']);self.assertIn('feature-fixture',reply['reply'])
        self.assertNotIn('secret',reply['reply'])
        with self.app.store.connect() as db:
            self.assertEqual(0,db.execute('SELECT count(*) FROM entries').fetchone()[0])
            self.assertEqual('android:'+body['phoneDevice'],db.execute('SELECT client FROM context_route_requests').fetchone()[0])


if __name__=='__main__':unittest.main()
