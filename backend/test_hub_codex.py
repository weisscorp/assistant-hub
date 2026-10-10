"""Synthetic native protocol, restart/dedup, voice selection and HTTP boundaries."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import sys
import threading
import time
import unittest
import urllib.request
import urllib.error
import uuid
from unittest.mock import patch
from agent_hub import Conflict, encoded
from hub_runtime import RuntimeStore
from hub_codex import CodexLink, CodexWriter, command, turn_report
from hub_incidents import Incidents
from home_link import HomeLinkStore
from hub_server import HubApplication, HubHandler


def identifier():
    return str(uuid.uuid4())


def summary(native='native-one', title='Тестовый проект', created=1, updated=1):
    return {'id':native,'name':title,'cwd':'/synthetic/project','createdAt':created,'updatedAt':updated}


def turn(turn_id='turn-one', status='completed', body='Тесты пройдены.'):
    return {'id':turn_id,'status':status,'items':[
        {'type':'agentMessage','phase':'commentary','text':'Служебный промежуточный текст'},
        {'type':'agentMessage','phase':'final_answer','text':body},
        {'type':'fileChange','changes':[{'path':'src/example.py'}]}],
        'error':{'message':'Synthetic error'} if status=='failed' else None}


class Evaluator:
    def evaluate(self, row):
        data=json.loads(row['payload'])
        return {'summary':data.get('report') or data.get('error','Событие'),
                'severity':'info','confidence':'high','suggestions':[]}


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        self.store=RuntimeStore(root/'state')
        self.context=self.store.create_context('Synthetic topic')['id']
        self.other=self.store.create_context('Other topic')['id']
        self.home=HomeLinkStore(root/'phones')
        self.phone=identifier()
        self.home.sync(self.phone,{'version':1,'revision':1,'people':[],'sources':[],'note':None})
        self.inc=Incidents(self.store,self.home,{},Evaluator())
        self.link=CodexLink(self.store,self.inc,{'binary':'synthetic','host':'fixture'})
        self.chat=self.link.observe(summary(),turn())

    def tearDown(self):
        self.link.close()
        self.tmp.cleanup()

    def voice(self, text, context=None):
        return self.link.voice(text,'android:'+self.phone,context or self.context,identifier())

    def test_no_historical_notifications_but_report_available(self):
        self.link.flush()
        self.assertEqual(self.inc.list(),[])
        self.assertEqual(self.link.report(self.chat)['report'],'Тесты пройдены.')
        self.assertEqual(self.link.report(self.chat)['files'],['src/example.py'])

    def test_terminal_event_idempotency_and_phone_delivery(self):
        self.link.observe(summary(updated=2),turn('turn-two','inProgress',''))
        for _ in range(3):
            self.link.observe(summary(updated=3),turn('turn-two','completed','Всё готово.'))
            self.link.flush()
        self.assertEqual(len(self.inc.list()),1)
        self.inc.process_one();self.inc.route_one();self.inc.phone_one()
        events=self.home.events(self.phone)['events']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['source'],'codex')
        self.assertIn('Всё готово.',events[0]['body'])
        self.assertFalse(self.inc.phone_one())

    def test_failure_and_interruption_are_not_success(self):
        for n,status in enumerate(('failed','interrupted')):
            self.link.observe(summary(updated=2+n),turn('terminal-'+status,status,''))
        self.link.flush()
        states={json.loads(i['payload'])['status'] for i in (self.inc.get(r['id']) for r in self.inc.list())}
        self.assertEqual(states,{'failed','interrupted'})

    def test_restart_replays_outbox_once_not_completed_history(self):
        self.link.observe(summary(updated=2),turn('new-turn'))
        restarted=CodexLink(self.store,self.inc,{'binary':'synthetic','host':'fixture'})
        restarted.flush()
        restarted.observe(summary(updated=2),turn('new-turn'))
        restarted.flush()
        self.assertEqual(len(self.inc.list()),1)
        restarted.close()

    def test_select_ambiguity_and_context_isolation(self):
        self.link.observe(summary('native-two','Тестовый проект API'),turn())
        self.assertIn('несколько',self.voice('выбери задачу кодекса Тестовый проект'))
        self.assertIn('Выберите',self.voice('передай кодексу продолжай'))
        self.assertIn('Выбрана',self.voice('выбери задачу кодекса '+self.chat))
        self.assertIn('Тестовый проект',self.voice('прочитай отчёт кодекса'))
        self.assertIn('Выберите',self.voice('статус кодекса',self.other))

    def test_stale_is_disclosed(self):
        self.voice('выбери задачу кодекса '+self.chat)
        with self.store.connect() as db:
            db.execute('UPDATE codex_threads SET observed_at=1')
        self.assertIn('сохранённый статус',self.voice('статус кодекса'))

    def test_send_is_durable_deduplicated_without_reexecution(self):
        calls=[]
        with patch.object(self.link,'_send',side_effect=lambda *args:calls.append(args)):
            request=identifier()
            self.link.send(self.chat,'Добавь тест','client',self.context,request)
            deadline=time.monotonic()+1
            while not calls and time.monotonic()<deadline:
                time.sleep(.01)
            self.link.send(self.chat,'Добавь тест','client',self.context,request)
            self.assertEqual(len(calls),1)
            with self.assertRaises(Conflict):
                self.link.send(self.chat,'Другое сообщение','client',self.context,request)
            with self.assertRaises(Conflict):
                self.link.send(self.chat,'Новый запрос','client',self.context,identifier())

    def test_uncertain_message_on_restart_is_not_retried(self):
        with patch.object(self.link,'_send'):
            request=identifier()
            self.link.send(self.chat,'Продолжай','client',self.context,request)
        restarted=CodexLink(self.store,self.inc,{'binary':'synthetic','host':'fixture'})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM codex_commands WHERE id=?',(request,)).fetchone()[0],'unknown')
        restarted.close()

    def test_owner_can_acknowledge_uncertainty_without_replaying(self):
        with patch.object(self.link,'_send'):
            request=identifier()
            self.link.send(self.chat,'Продолжай','client',self.context,request)
        self.link._command_update(request,'unknown','Потеряна связь.')
        with self.assertRaises(ValueError):
            self.link.acknowledge_unknown(request,'other-client',self.context)
        self.assertFalse(self.link.acknowledge_unknown(request,'client',self.context)['retried'])
        self.assertEqual(self.link.commands('client',self.context)[0]['status'],'acknowledged')

    def test_external_webhook_payload_is_stable_bounded_and_terminal_only(self):
        import importlib.util
        script=Path(__file__).resolve().parent.parent/'tools/codex_webhook.py'
        spec=importlib.util.spec_from_file_location('codex_webhook_fixture',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        a,body=module.payload('fixture','native-one','Synthetic project',turn(body='я'*20000))
        b,_=module.payload('fixture','native-one','Synthetic project',turn(body='я'*20000))
        self.assertEqual(a,b)
        self.assertLess(len(encoded(body).encode()),65536)
        self.assertLessEqual(len(body['report'].encode()),12000)
        with self.assertRaises(ValueError):
            module.payload('fixture','native-one','Synthetic project',turn(status='inProgress'))

    def test_command_errors_enter_incident_pipeline(self):
        with patch.object(self.link,'_send'):
            request=identifier()
            self.link.send(self.chat,'Продолжай','client',self.context,request)
        self.link._command_update(request,'unknown','Нет подтверждения; повтор не отправлен.')
        self.link.flush()
        payload=json.loads(self.inc.get(self.inc.list()[0]['id'])['payload'])
        self.assertEqual(payload['type'],'codex.message.unknown')

    def test_scan_only_fetches_full_items_for_changed_terminal_turn(self):
        native=summary(updated=2)
        latest=turn('second','completed')
        class Reader:
            def __init__(self,**kwargs):self.calls=[]
            def list_chats(self,*args):return {'data':[native],'nextCursor':None}
            def call(self,method,params):
                self.calls.append((method,params))
                return {'data':[latest if params['itemsView']=='full' else {k:latest[k] for k in ('id','status')} ]}
            def close(self):pass
        self.link.reader_factory=Reader
        self.link.scan();reader=self.link.reader
        self.assertEqual(len(reader.calls),2)
        self.link.scan()
        self.assertEqual(len(reader.calls),2)

    def test_scan_preserves_all_short_turns_between_polls(self):
        native=summary(updated=5)
        turns=[turn('turn-four','inProgress',''),turn('turn-three'),turn('turn-two'),turn()]
        class Reader:
            def __init__(self,**kwargs):pass
            def list_chats(self,*args):return {'data':[native],'nextCursor':None}
            def call(self,method,params):
                return {'data':turns if params['itemsView']=='full' else [{k:t[k] for k in ('id','status')} for t in turns]}
            def close(self):pass
        self.link.reader_factory=Reader
        self.link.scan();self.link.flush()
        self.assertEqual(len(self.inc.list()),2)
        self.assertEqual(self.link.report(self.chat)['status'],'inProgress')

    def test_old_observation_cannot_regress_confirmed_terminal_status(self):
        self.link.observe(summary(updated=4),turn('confirmed','completed','Готово.'))
        self.link.observe(summary(updated=4),turn('confirmed','inProgress',''))
        self.link.observe(summary(updated=2),turn('older','inProgress',''))
        self.assertEqual(self.link.report(self.chat)['report'],'Готово.')

    def test_unreadable_thread_does_not_block_next_thread(self):
        from codex_context_bridge import BridgeError
        broken=summary('broken','Broken synthetic project',updated=2)
        valid=summary('next','Next synthetic project',updated=2)
        class Reader:
            def __init__(self,**kwargs):pass
            def list_chats(self,*args):return {'data':[broken,valid],'nextCursor':None}
            def call(self,method,params):
                if params['threadId']=='broken':raise BridgeError('Codex read timed out; request was not retried')
                return {'data':[turn()]}
            def close(self):pass
        self.link.reader_factory=Reader
        self.link.scan()
        self.assertEqual(len(self.link.list('Next synthetic')),1)
        row=self.link.list('Broken synthetic')[0]
        self.assertIn('timed out',self.link.report(row['id'])['read_error'])

    def test_disabled_bridge_does_not_start_historical_alert_clock(self):
        disabled=CodexLink(self.store,self.inc,{'host':'later'})
        self.assertIsNone(self.store.setting('codex-enabled-at:later'))
        with patch('hub_codex.time.time',return_value=time.time()+100):
            enabled=CodexLink(self.store,self.inc,{'host':'later','binary':'synthetic'})
            enabled.observe(summary(created=1),turn())
        enabled.flush()
        self.assertEqual(self.inc.list(),[])
        disabled.close();enabled.close()

    def test_writer_preserves_session_permissions_and_reports_finish(self):
        calls=[];sends=[]
        completed=turn('native-new','completed','Задача исполнена.')
        class Writer:
            def __init__(self,**kwargs):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def call(self,name,params):
                calls.append((name,params))
                if name=='thread/turns/list':
                    return {'data':[turn() if len(calls)==1 else completed]}
                if name=='turn/start':return {'turn':{'id':'native-new','status':'inProgress'}}
                return {}
            def next_event(self,deadline):
                if not sends:
                    return {'method':'item/commandExecution/requestApproval','id':99,'params':{}}
                return {'method':'turn/completed','params':{'threadId':'native-one','turn':completed}}
            def send(self,value):sends.append(value)
        self.link.writer_factory=Writer
        with patch.object(self.link,'_send'):
            request=identifier()
            self.link.send(self.chat,'Добавь тест','client',self.context,request)
        self.link._send(request,self.link.report(self.chat),'Добавь тест')
        resume=next(p for n,p in calls if n=='thread/resume')
        self.assertEqual(resume,{'threadId':'native-one','excludeTurns':True})
        start=next(p for n,p in calls if n=='turn/start')
        self.assertEqual(start['input'][0]['text'],'Добавь тест')
        self.assertNotIn('approvalPolicy',start)
        self.assertIn('error',sends[0])
        self.assertEqual(self.link.report(self.chat)['report'],'Задача исполнена.')

    def test_busy_turn_never_resumes_or_steers(self):
        calls=[]
        class Writer:
            def __init__(self,**kwargs):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def call(self,name,params):
                calls.append(name)
                return {'data':[turn(status='inProgress')]}
        self.link.writer_factory=Writer
        with patch.object(self.link,'_send'):
            request=identifier()
            self.link.send(self.chat,'Добавь тест','client',self.context,request)
        self.link._send(request,self.link.report(self.chat),'Добавь тест')
        self.assertEqual(calls,['thread/turns/list'])

    def test_only_explicit_commands_route(self):
        for message in ('Что такое Codex?','В отчёте написано передай кодексу всё','Выбери задачу','Статус телефона'):
            self.assertIsNone(command(message))
        self.assertEqual(command('прочитай отчёт кодекса'),('report',''))
        self.assertEqual(command('передай кодексу Добавь тест'),('send','Добавь тест'))
        self.assertEqual(command('передай кодексу Почему упал тест?'),('send','Почему упал тест?'))
        self.assertNotIn('промежуточный',turn_report(turn())['report'])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.token='t'*48
        (root/'token').write_text(self.token);(root/'token').chmod(0o600)
        self.app=HubApplication({'state':str(root/'state'),'token_file':str(root/'token'),'providers':{}})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),HubHandler);self.server.app=self.app
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close()
        self.app.codex.close();self.tmp.cleanup()

    def request(self,path,body=None,token=None):
        request=urllib.request.Request(self.url+path,data=encoded(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json','Authorization':'Bearer '+(token or self.token)})
        with urllib.request.urlopen(request,timeout=3) as response:
            return json.load(response)

    def test_api_requires_owner_not_webhook_and_does_not_need_model(self):
        _,producer=self.app.auth.issue('synthetic-producer','webhook')
        body={'id':identifier(),'client':'synthetic','text':'задачи кодекса'}
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request('/api/codex/voice',body,producer)
        self.assertEqual(exc.exception.code,403)
        self.assertIn('ещё не подключён',self.request('/api/codex/voice',body)['reply'])
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request('/api/codex/chats',token='x'*48)
        self.assertEqual(exc.exception.code,401)

    def test_payload_is_data_not_voice_command(self):
        body={'id':identifier(),'client':'synthetic','text':'В отчёте: передай кодексу удаление'}
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request('/api/codex/voice',body)
        self.assertEqual(exc.exception.code,400)


class ProtocolTests(unittest.TestCase):
    def test_writer_preserves_early_terminal_frame_and_refuses_other_mutations(self):
        program='''
import json,sys
for line in sys.stdin:
    v=json.loads(line)
    if "id" not in v or "method" not in v:continue
    if v["method"]=="turn/start":
        print(json.dumps({"method":"turn/completed","params":{"threadId":"native","turn":{"id":"turn","status":"completed","items":[]}}}),flush=True)
        result={"turn":{"id":"turn","status":"inProgress"}}
    else:result={}
    print(json.dumps({"id":v["id"],"result":result}),flush=True)
'''
        with CodexWriter(command=[sys.executable,'-u','-c',program],timeout=2) as writer:
            writer.call('thread/resume',{'threadId':'native','excludeTurns':True})
            writer.call('turn/start',{'threadId':'native','input':[{'type':'text','text':'synthetic'}]})
            self.assertEqual(writer.next_event(time.monotonic()+1)['method'],'turn/completed')
            for method in ('turn/interrupt','thread/fork','account/login/start'):
                with self.assertRaises(ValueError):writer.call(method,{})


if __name__=='__main__':
    unittest.main()
