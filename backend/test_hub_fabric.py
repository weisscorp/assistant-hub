"""Node protocol and clocks with synthetic state; no device commands or sound."""
import json
import secrets
import threading
import tempfile
import unittest
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from agent_hub import Conflict
from hub_fabric import Fabric, object_schema
from hub_runtime import RuntimeStore, Coordinator
from hub_server import HubApplication, HubHandler
from hub_node import NodeClient, CAPABILITIES, handle


def key():return str(uuid.uuid4())


class FabricTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=RuntimeStore(Path(self.tmp.name)/'state')
        self.context=self.store.create_context('Synthetic topic')['id']
        self.other=self.store.create_context('Other topic')['id']
        self.now=1000
        self.fabric=Fabric(self.store,lambda:self.now)
        self.fabric.register('fixture-node','Synthetic node','fixture',CAPABILITIES)

    def tearDown(self):self.fabric.close();self.tmp.cleanup()

    def call(self,node='hub',capability='timer.create',arguments=None,id=None):
        return self.fabric.call(node,capability,arguments if arguments is not None else {'duration_seconds':5},self.context,'test',id or key())

    def test_timer_fires_once_survives_restart_and_is_topic_scoped(self):
        result=self.call()
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(self.fabric.timers(self.context)[0]['remaining_seconds'],5)
        self.assertEqual(self.fabric.timers(self.other),[])
        restarted=Fabric(self.store,lambda:self.now)
        self.now+=6;restarted.tick();restarted.tick()
        self.assertEqual(restarted.timers(self.context)[0]['status'],'fired')
        self.assertEqual(len(self.store.notifications()),1)
        self.assertEqual(len([e for e in self.store.events() if e['kind']=='timer.fired']),1)

    def test_retry_is_idempotent_and_conflicting_payload_rejected(self):
        id=key();one=self.call(id=id);two=self.call(id=id)
        self.assertEqual(one['result']['id'],two['result']['id'])
        self.assertEqual(len(self.fabric.timers(self.context)),1)
        with self.assertRaises(Conflict):self.call(id=id,arguments={'duration_seconds':9})

    def test_cancel_timer_does_not_notify_and_foreign_topic_cannot_cancel(self):
        timer=self.call()['result']
        foreign=self.fabric.call('hub','timer.cancel',{'timer_id':timer['id']},self.other,'test',key())
        self.assertEqual(foreign['status'],'failed')
        self.assertEqual(self.call(capability='timer.cancel',arguments={'timer_id':timer['id']})['result']['status'],'cancelled')
        self.now+=9;self.fabric.tick()
        self.assertEqual(self.store.notifications(),[])

    def test_invalid_arguments_never_create_call_or_timer(self):
        for args in ({'duration_seconds':True},{'duration_seconds':0},{'duration_seconds':604801},
                     {'duration_seconds':5,'unexpected':1},{'duration_seconds':'5'},{}):
            with self.assertRaises(ValueError):self.call(arguments=args)
        self.assertEqual(self.fabric.calls(self.context),[])
        self.assertEqual(self.fabric.timers(self.context),[])

    def test_discovery_does_not_invent_actions_and_tracks_offline_nodes(self):
        self.assertEqual(self.fabric.nodes('timer',True)[0]['id'],'hub')
        self.assertEqual(self.fabric.nodes('not-advertised',True),[])
        self.now+=91
        self.assertFalse(next(n for n in self.fabric.nodes() if n['id']=='fixture-node')['online'])
        with self.assertRaises(ValueError):self.call('fixture-node','timer.create')

    def test_node_id_and_label_discovery_return_the_nodes_schemas(self):
        self.assertEqual(self.fabric.nodes('fixture-node',True)[0]['id'],'fixture-node')
        self.assertEqual(len(self.fabric.nodes('Synthetic node',True)[0]['capabilities']),2)
        self.assertEqual(self.fabric.nodes('время',True)[0]['capabilities'][0]['name'],'system.time')

    def test_catalog_is_bounded_even_with_many_large_descriptors(self):
        actions=[{'name':'action.'+str(i),'description':'x'*800,'inputSchema':object_schema()} for i in range(32)]
        for i in range(9):self.fabric.register('large-'+str(i),'Synthetic large catalog','fixture',actions)
        catalog=self.fabric.nodes('',True)
        self.assertLess(len(json.dumps(catalog,ensure_ascii=False).encode()),14500)
        self.assertTrue(any(n.get('truncated') for n in catalog))
        self.assertEqual(self.fabric.nodes('action.31',True)[0]['capabilities'][0]['name'],'action.31')

    def test_invalid_bound_enum_and_non_json_result_are_rejected(self):
        for schema in ({'type':'integer','minimum':3,'maximum':2},{'type':'string','maxLength':-1},
                       {'type':'integer','enum':[True]},{'type':'string','enum':[{'nested':1}]}):
            with self.assertRaises(ValueError):
                self.fabric.register('bad','Fixture','fixture',[{'name':'bad.action','description':'Fixture','inputSchema':object_schema({'value':schema})}])
        item=self.call('fixture-node','system.info',{});claim=self.fabric.claim('fixture-node')
        with self.assertRaises(ValueError):self.fabric.complete('fixture-node',item['id'],claim['lease'],{'value':float('nan')})

    def test_fired_timer_cancel_reports_fired_without_a_cancel_event(self):
        timer=self.call()['result'];self.now+=6;self.fabric.tick()
        result=self.call(capability='timer.cancel',arguments={'timer_id':timer['id']})
        self.assertEqual(result['result']['status'],'fired')
        self.assertFalse(any(e['kind']=='timer.cancelled' for e in self.store.events()))

    def test_claim_is_atomic_even_for_two_executors_and_ack_retry_is_safe(self):
        result=self.call('fixture-node','system.info',{})
        claimed=[]
        threads=[threading.Thread(target=lambda:claimed.append(self.fabric.claim('fixture-node'))) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(len([c for c in claimed if c]),1)
        item=next(c for c in claimed if c)
        done=self.fabric.complete('fixture-node',result['id'],item['lease'],{'value':'fixture'})
        self.assertEqual(done['status'],'succeeded')
        self.assertEqual(self.fabric.complete('fixture-node',result['id'],item['lease'],{'value':'fixture'}),done)
        with self.assertRaises(Conflict):self.fabric.complete('fixture-node',result['id'],key(),{})
        with self.assertRaises(ValueError):self.fabric.result(result['id'],self.other)

    def test_expired_claim_becomes_unknown_without_reexecution(self):
        item=self.call('fixture-node','system.info',{})
        claim=self.fabric.claim('fixture-node');self.now+=61;self.fabric.tick()
        self.assertEqual(self.fabric.result(item['id'])['status'],'unknown')
        self.assertIsNone(self.fabric.claim('fixture-node'))
        with self.assertRaises(Conflict):self.fabric.complete('fixture-node',item['id'],claim['lease'],{})

    def test_unclaimed_action_expires_and_parent_cancel_stops_only_queued(self):
        id=key();task=key()
        self.fabric.call('fixture-node','system.info',{},self.context,'test',id,task)
        self.fabric.cancel_task(task)
        self.assertEqual(self.fabric.result(id)['status'],'cancelled')
        item=self.call('fixture-node','system.info',{})
        self.now+=301;self.fabric.tick()
        self.assertEqual(self.fabric.result(item['id'])['status'],'failed')
        self.assertIsNone(self.fabric.claim('fixture-node'))

    def test_restart_keeps_unclaimed_but_never_replays_dispatched(self):
        dispatched=self.call('fixture-node','system.info',{});self.fabric.claim('fixture-node')
        queued=self.call('fixture-node','system.info',{})
        resumed=Fabric(self.store,lambda:self.now)
        self.assertEqual(resumed.result(dispatched['id'])['status'],'unknown')
        self.assertEqual(resumed.claim('fixture-node')['id'],queued['id'])

    def test_schema_reserved_node_and_duplicate_capability_rejected(self):
        for capabilities in ([CAPABILITIES[0],CAPABILITIES[0]],
                             [{'name':'unsafe','description':'fixture','inputSchema':{'type':'object'}}],
                             [{'name':'unsafe','description':'fixture','inputSchema':None}]):
            with self.assertRaises(ValueError):self.fabric.register('bad','Fixture','fixture',capabilities)
        with self.assertRaises(ValueError):self.fabric.register('hub','Fixture','fixture',CAPABILITIES)

    def test_model_tools_can_act_and_reuse_request_key(self):
        coordinator=Coordinator(self.store,{'fixture':object(),'another-agent':object()})
        row=self.store.create_task(self.context,'test','fixture','Поставь таймер на 5 секунд',key())[0]
        args={'node':'hub','capability':'timer.create','arguments_json':'{"duration_seconds":5}','request_key':'timer-1'}
        one=coordinator.call_tool('capability_call',args,row)
        two=coordinator.call_tool('capability_call',args,row)
        self.assertEqual(one['id'],two['id'])
        self.assertEqual(one['status'],'succeeded')
        self.assertEqual({p['id'] for p in coordinator.call_tool('agents_list',{},row)},{'fixture','another-agent'})
        self.assertEqual(coordinator.call_tool('capabilities_list',{'query':'timer'},row)[0]['id'],'hub')

    def test_model_json_arguments_are_validated_before_any_action(self):
        coordinator=Coordinator(self.store,{})
        row=self.store.create_task(self.context,'test','fixture','Synthetic request',key())[0]
        base={'node':'hub','capability':'timer.create','request_key':'timer-1'}
        for wire in ('not JSON','[]','null','{}','{"duration_seconds":true}'):
            result=coordinator.call_tool('capability_call',dict(base,arguments_json=wire),row)
            self.assertIn('error',result)
        self.assertEqual(self.fabric.calls(self.context),[])

    def test_model_receives_current_bounded_node_preview_after_other_tool_history(self):
        self.store.append(self.context,'tool','Synthetic earlier timer catalog','fixture')
        coordinator=Coordinator(self.store,{})
        row=self.store.create_task(self.context,'test','fixture','Узнай время на fixture-node',key())[0]
        packet=coordinator.context_packet(row,4096)
        self.assertEqual(packet['node_hints'][0]['id'],'fixture-node')
        self.assertIn('system.time',packet['node_hints'][0]['capabilities'])
        self.assertLessEqual(len(json.dumps(packet,ensure_ascii=False,separators=(',',':')).encode()),4096)
        self.assertLessEqual(len(json.dumps(self.fabric.hints(row['prompt']),ensure_ascii=False).encode()),1500)

    def test_ollama_history_keeps_roles_and_drops_old_discovery_snapshots(self):
        from hub_providers import messages
        self.store.append(self.context,'user','Earlier synthetic request','fixture')
        self.store.append(self.context,'assistant','Earlier synthetic reply','fixture')
        self.store.append(self.context,'tool',json.dumps({'name':'capabilities_list','result':['old-catalog']}),'fixture')
        self.store.append(self.context,'tool',json.dumps({'name':'capability_call','result':{'status':'succeeded'}}),'fixture')
        coordinator=Coordinator(self.store,{})
        row=self.store.create_task(self.context,'test','fixture','Current synthetic request',key())[0]
        turns=messages(row,coordinator)
        self.assertEqual(turns[-1],{'role':'user','content':'Current synthetic request'})
        self.assertIn({'role':'assistant','content':'Earlier synthetic reply'},turns)
        self.assertIn('node_hints',turns[-2]['content'])
        self.assertNotIn('old-catalog',json.dumps(turns))
        self.assertIn('succeeded',turns[-2]['content'])

    def test_named_nodes_get_typed_tools_that_share_validation_and_idempotency(self):
        coordinator=Coordinator(self.store,{})
        row=self.store.create_task(self.context,'test','fixture','Поставь таймер в hub',key())[0]
        tools=coordinator.node_tools(row)
        name=next(n for n,t in tools.items() if t[2]=='timer.create')
        args={'duration_seconds':5,'label':'Synthetic timer'}
        one=coordinator.call_tool(name,args,row);two=coordinator.call_tool(name,args,row)
        self.assertEqual(one['status'],'succeeded');self.assertEqual(one['id'],two['id'])
        self.assertIn('error',coordinator.call_tool(name,{'duration_seconds':True},row))
        self.assertEqual(len(self.fabric.timers(self.context)),1)
        self.assertEqual(coordinator.node_tools(dict(row,prompt='Generic synthetic conversation')), {})


class FabricHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.token=root/'token';self.token.write_text(secrets.token_urlsafe(48));self.token.chmod(0o600)
        self.app=HubApplication({'state':str(root/'state'),'token_file':str(self.token),'providers':{}})
        self.context=self.app.store.contexts()[0]['id']
        self.server=ThreadingHTTPServer(('127.0.0.1',0),HubHandler);self.server.app=self.app
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=NodeClient('http://127.0.0.1:'+str(self.server.server_port),self.token)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.app.fabric.close();self.tmp.cleanup()

    def test_real_http_node_worker_returns_result_and_can_read_bounded_context(self):
        self.client.request('/api/nodes',{'id':'fixture','label':'Fixture','kind':'test','capabilities':CAPABILITIES})
        row=self.client.request('/api/capabilities/call',{'id':key(),'node':'fixture','capability':'system.info','arguments':{},'context_id':self.context,'client':'test'})
        self.assertEqual(row['status'],'queued')
        self.assertTrue(self.client.execute_once('fixture',CAPABILITIES,handle))
        result=self.client.request('/api/capabilities/calls/'+row['id']+'?context='+self.context)
        self.assertEqual(result['status'],'succeeded')
        self.assertIn('system',result['result'])
        self.assertFalse(self.client.execute_once('fixture',CAPABILITIES,handle))
        self.assertEqual(self.client.context_packet(self.context,'none')['memory'],[])

    def test_unauthenticated_and_coucou_clients_cannot_claim_nodes(self):
        for header in ({},{'Authorization':'Bearer ollama'}):
            request=urllib.request.Request(self.client.url+'/api/nodes',headers=header)
            with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code,401)

    def test_worker_retries_acknowledgement_without_running_handler_twice(self):
        self.client.request('/api/nodes',{'id':'fixture','label':'Fixture','kind':'test','capabilities':CAPABILITIES})
        self.client.request('/api/capabilities/call',{'id':key(),'node':'fixture','capability':'system.info','arguments':{},'context_id':self.context,'client':'test'})
        actual=self.client.request;executed=[];failed=[False]
        def request(path,body=None):
            if path.endswith('/result') and not failed[0]:failed[0]=True;raise OSError('synthetic transport failure')
            return actual(path,body)
        def handler(*args):executed.append(1);return {'ack':True}
        with patch.object(self.client,'request',side_effect=request),patch('hub_node.time.sleep'):
            self.assertTrue(self.client.execute_once('fixture',CAPABILITIES,handler))
        self.assertEqual(executed,[1])


if __name__=='__main__':unittest.main()
