"""Coucou facade -> model tools -> persistent timer -> leased mascot notice.

All state, model streams and socket writes here are synthetic.
"""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from agent_hub import Conflict
from home_link import HomeLinkStore
from hub_desktop import deliver
from hub_fabric import Fabric
from hub_incidents import Incidents
from hub_notifications import DesktopNotices
from hub_node import NodeClient
from hub_runtime import RuntimeStore
from hub_runtime import Coordinator
from hub_providers import OllamaProvider
from hub_server import HubApplication, HubHandler


def uid():return str(uuid.uuid4())


class TimerNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=RuntimeStore(self.root/'state');self.context=self.store.create_context('Fixture')['id']
        self.inc=Incidents(self.store,HomeLinkStore(self.root/'link'),{})
        self.now=1000;self.fabric=Fabric(self.store,lambda:self.now)
        self.client='desktop:fixture'

    def tearDown(self):self.fabric.close();self.tmp.cleanup()

    def fire(self):
        self.fabric.call('hub','timer.create',{'duration_seconds':5,'label':'Fixture'},self.context,'coucou',uid())
        self.now+=6;self.fabric.tick()
        return self.store.notifications()[-1]['id']

    def register(self):self.inc.presence(self.client,'desktop','Fixture',True)

    def test_timer_reaches_mascot_once_without_incident_or_topic_switch(self):
        self.register();notice=self.fire()
        self.assertEqual(self.inc.notices.queue(self.inc.clients()),1)
        self.assertEqual(self.inc.notices.queue(self.inc.clients()),0)
        event=self.inc.claim(self.client)
        self.assertEqual(event['notification_id'],notice)
        self.assertEqual(event['body'],'Таймер «Fixture» завершён.')
        class Client:
            def request(_,path,body):
                self.assertEqual(path,'/api/deliveries/receipt')
                return self.inc.receipt(body['client'],body['id'],body['status'],body['lease'],body['coucou_written'])
        with patch('hub_desktop.write_event',return_value=True) as write:
            deliver(Client(),self.client,event,self.root/'fixture.sock',uid())
        self.assertEqual(write.call_count,2)
        self.assertEqual([json.loads(c.args[1])['hook_event_name'] for c in write.call_args_list],['SessionStart','Stop'])
        self.assertIsNone(self.inc.claim(self.client))
        self.assertEqual(self.inc.list(),[])
        self.assertEqual(len(self.store.contexts()),1)
        self.assertIsNone(self.store.focus('coucou')['context_id'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM desktop_notice_deliveries').fetchone()[0],'submitted')

    def test_wait_for_desktop_and_preserve_queued_notice_across_restart(self):
        self.fire();self.assertEqual(self.inc.notices.queue([]),0)
        self.register();self.inc.notices.queue(self.inc.clients())
        self.inc.notices=DesktopNotices(self.store);self.inc.recover()
        self.assertIsNotNone(self.inc.claim(self.client))

    def test_upgrade_does_not_emit_historical_alerts(self):
        # Simulate the first upgrade on an older Hub database.
        with self.store.connect() as db:
            db.execute('DROP TABLE desktop_notice_deliveries');db.execute('DROP TABLE desktop_notice_cursor')
        self.fire();self.inc.notices=DesktopNotices(self.store);self.register()
        self.assertEqual(self.inc.notices.queue(self.inc.clients()),0)
        self.fire();self.assertEqual(self.inc.notices.queue(self.inc.clients()),1)

    def test_lease_receipt_validation_and_uncertain_delivery_never_replayed(self):
        self.register();self.fire();self.inc.notices.queue(self.inc.clients())
        event=self.inc.claim(self.client)
        self.assertFalse(self.inc.receipt('desktop:other',event['id'],'submitted',event['lease'],True)['tracked'])
        with self.assertRaises(Conflict):self.inc.receipt(self.client,event['id'],'submitted',uid(),True)
        with patch('hub_notifications.time.time',return_value=time.time()+61):
            with self.assertRaises(Conflict):self.inc.receipt(self.client,event['id'],'submitted',event['lease'],True)
            self.assertIsNone(self.inc.claim(self.client))
        self.inc.recover();self.assertIsNone(self.inc.claim(self.client))

    def test_concurrent_claim_and_idempotent_receipt(self):
        self.register();self.fire();self.inc.notices.queue(self.inc.clients())
        with ThreadPoolExecutor(5) as pool:events=list(pool.map(lambda _:self.inc.claim(self.client),range(5)))
        self.assertEqual(sum(e is not None for e in events),1)
        event=next(e for e in events if e)
        for _ in range(2):self.assertTrue(self.inc.receipt(self.client,event['id'],'submitted',event['lease'],True)['tracked'])
        with self.assertRaises(Conflict):self.inc.receipt(self.client,event['id'],'failed',event['lease'],False)

    def test_restart_after_socket_claim_does_not_send_again(self):
        self.register();self.fire();self.inc.notices.queue(self.inc.clients())
        self.assertIsNotNone(self.inc.claim(self.client))
        self.inc.notices=DesktopNotices(self.store);self.inc.recover()
        self.assertIsNone(self.inc.claim(self.client))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM desktop_notice_deliveries').fetchone()[0],'unknown')


class CoucouTimerHTTPTests(unittest.TestCase):
    def test_existing_provider_facade_clarifies_then_calls_typed_tool(self):
        captured=[]
        class Model(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append(body)
                if len(captured)==1:
                    message={'role':'assistant','content':'Да. На сколько поставить таймер?'}
                elif len(captured)==2:
                    name=next(t['function']['name'] for t in body['tools'] if t['function']['name'].startswith('node_hub_timer_create_'))
                    message={'role':'assistant','content':'','tool_calls':[{'function':{'name':name,'arguments':{'duration_seconds':5,'label':'Fixture'}}}]}
                else:message={'role':'assistant','content':'Таймер на пять секунд поставлен.'}
                raw=(json.dumps({'message':message,'done':True})+'\n').encode()
                self.send_response(200);self.send_header('Content-Type','application/x-ndjson');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);token=root/'token';token.write_text(secrets.token_urlsafe(48));token.chmod(0o600)
            model=ThreadingHTTPServer(('127.0.0.1',0),Model);threading.Thread(target=model.serve_forever,daemon=True).start()
            app=HubApplication({'state':str(root/'state'),'token_file':str(token),'coucou_capability':'synthetic-capability',
                'providers':{'ollama':{'type':'ollama','model':'fixture','url':'http://127.0.0.1:'+str(model.server_port)}}})
            hub=ThreadingHTTPServer(('127.0.0.1',0),HubHandler);hub.app=app;threading.Thread(target=hub.serve_forever,daemon=True).start()
            client=NodeClient('http://127.0.0.1:'+str(hub.server_port),token)
            try:
                import urllib.request
                def ask(prompt):
                    body={'model':'assistant-ollama','messages':[{'role':'user','content':prompt}]}
                    request=urllib.request.Request(client.url+'/coucou/synthetic-capability/v1/chat/completions',data=json.dumps(body).encode(),
                        headers={'Authorization':'Bearer ollama','Content-Type':'application/json'})
                    with urllib.request.urlopen(request,timeout=5) as reply:return json.load(reply)['choices'][0]['message']['content']
                self.assertIn('На сколько',ask('таймер можешь поставить?'))
                context=app.client_focus('coucou')['context_id']
                self.assertEqual(app.fabric.timers(context),[])
                self.assertIn('поставлен',ask('на пять секунд'))
                timers=app.fabric.timers(context);self.assertEqual(len(timers),1);self.assertEqual(timers[0]['duration_seconds'],5)
                self.assertIn({'role':'assistant','content':'Да. На сколько поставить таймер?'},captured[1]['messages'])
                self.assertEqual(json.loads(captured[2]['messages'][-1]['content'])['status'],'succeeded')
                client.request('/api/presence',{'id':'desktop:fixture','kind':'desktop','label':'Fixture','active':True})
                with patch.object(app.fabric,'clock',return_value=timers[0]['due_at']+1):app.fabric.tick()
                app.incidents.notices.queue(app.incidents.clients())
                event=client.request('/api/deliveries/claim',{'client':'desktop:fixture'})
                self.assertEqual(event['body'],'Таймер «Fixture» завершён.')
                receipt=client.request('/api/deliveries/receipt',{'client':'desktop:fixture','id':event['id'],'lease':event['lease'],'status':'submitted','coucou_written':True})
                self.assertTrue(receipt['tracked'])
                self.assertIsNone(client.request('/api/deliveries/claim',{'client':'desktop:fixture'}))
            finally:
                hub.shutdown();hub.server_close();model.shutdown();model.server_close();app.fabric.close()


class ModelDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=RuntimeStore(Path(self.tmp.name)/'state')
        self.context=self.store.create_context('Fixture')['id'];self.coordinator=Coordinator(self.store,{})
        self.row=self.store.create_task(self.context,'coucou','ollama','отмени таймер',uid())[0]
        self.provider=OllamaProvider('http://example.invalid','fixture')
    def tearDown(self):self.coordinator.fabric.close();self.tmp.cleanup()
    def run_model(self,messages):
        streams=[io.BytesIO((json.dumps({'done':True,'message':m})+'\n').encode()) for m in messages]
        progress=[]
        with patch('hub_providers.urllib.request.urlopen',side_effect=streams) as requests:
            reply=self.provider.run(self.row,self.coordinator,threading.Event(),progress.append)
        self.requests=[json.loads(c.args[0].data) for c in requests.call_args_list]
        return reply,progress
    def test_refusal_draft_is_reviewed_without_creating_a_timer(self):
        reply,progress=self.run_model([{'content':'Я не могу ставить таймеры.'},
            {'content':json.dumps({'name':'capabilities_list','arguments':{'query':'timer'}})},
            {'content':'Да. На сколько поставить таймер?'}])
        self.assertEqual(reply,'Да. На сколько поставить таймер?')
        self.assertEqual(progress,['Да. На сколько поставить таймер?'])
        self.assertEqual(self.coordinator.fabric.timers(self.context),[])
        self.assertNotIn('tools',self.requests[1])
        self.assertEqual({s['properties']['name']['enum'][0] for s in self.requests[1]['format']['oneOf']},
                         {'capabilities_list','agents_list'})
    def test_claimed_cancellation_requires_actual_model_tool_execution(self):
        timer=self.coordinator.fabric.call('hub','timer.create',{'duration_seconds':300},self.context,'fixture',uid())['result']
        name=next(n for n,t in self.coordinator.node_tools(self.row).items() if t[2]=='timer.cancel')
        reply,progress=self.run_model([{'content':'Таймер отменён.'},
            {'content':json.dumps({'name':name,'arguments':{'timer_id':timer['id']}})},
            {'content':'Таймер отменён.'}])
        self.assertEqual(self.coordinator.fabric.timers(self.context)[0]['status'],'cancelled')
        self.assertEqual(progress,['Таймер отменён.'])
    def test_repeated_unverified_claim_fails_instead_of_reporting_success(self):
        with self.assertRaises(RuntimeError):self.run_model([{'content':'Таймер отменён.'},{'content':'Таймер отменён.'}])
        self.assertEqual(self.coordinator.fabric.timers(self.context),[])

    def test_reading_running_timer_does_not_confirm_cancellation(self):
        timer=self.coordinator.fabric.call('hub','timer.create',{'duration_seconds':300},self.context,'fixture',uid())['result']
        tools=self.coordinator.node_tools(self.row)
        listing=next(n for n,t in tools.items() if t[2]=='timer.list')
        cancel=next(n for n,t in tools.items() if t[2]=='timer.cancel')
        reply,progress=self.run_model([
            {'content':'','tool_calls':[{'function':{'name':listing,'arguments':{}}}]},
            {'content':'Таймер отменён.'},
            {'content':json.dumps({'name':cancel,'arguments':{'timer_id':timer['id']}})},
            {'content':'Таймер отменён.'}])
        self.assertEqual(self.coordinator.fabric.timers(self.context)[0]['status'],'cancelled')
        self.assertEqual(progress,['Таймер отменён.'])

    def test_vague_question_cannot_create_a_timer_with_invented_duration(self):
        self.row['prompt']='таймер можешь поставить?'
        name=next(n for n,t in self.coordinator.node_tools(self.row).items() if t[2]=='timer.create')
        reply,progress=self.run_model([
            {'content':'','tool_calls':[{'function':{'name':name,'arguments':{'duration_seconds':300}}}]},
            {'content':'На сколько поставить таймер?'}])
        self.assertEqual(self.coordinator.fabric.timers(self.context),[])
        self.assertEqual(reply,'На сколько поставить таймер?')

    def test_duration_word_forms_and_questions_without_a_duration(self):
        from hub_runtime import explicit_duration
        for prompt in ('на 5 минут','на пять минут','на двадцать три секунды','на полторы минуты','на час','на полчаса','на 00:05:00','for five minutes'):
            self.assertTrue(explicit_duration(prompt),prompt)
        for prompt in ('таймер можешь поставить?','сколько минут можно поставить?','поставь таймер','на 5'):
            self.assertFalse(explicit_duration(prompt),prompt)

    def test_creation_cannot_claim_completion_notice_already_sent(self):
        self.row['prompt']='Поставь таймер на пять минут'
        name=next(n for n,t in self.coordinator.node_tools(self.row).items() if t[2]=='timer.create')
        reply,progress=self.run_model([
            {'content':'','tool_calls':[{'function':{'name':name,'arguments':{'duration_seconds':300}}}]},
            {'content':'Таймер поставлен. Уведомление отправлено в тему и у маскота Coucou.'}])
        self.assertEqual(reply,'Таймер поставлен. Уведомление придёт по завершении таймера.')
        self.assertEqual(self.store.notifications(),[])


if __name__=='__main__':unittest.main()
