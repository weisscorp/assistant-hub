"""Owned dependency lifecycle fixtures; never launch daemons or contact devices."""
import copy
import io
import json
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import local_stack as stack


class ManagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'logs').mkdir(mode=0o700)
        (self.root/'runtime').mkdir(mode=0o700)
        self.node=self.root/'node';self.ollama=self.root/'ollama';self.cli=self.root/'cli.mjs'
        for path in (self.node,self.ollama,self.cli):path.write_text('synthetic fixture\n');path.chmod(0o700)
        self.models=self.root/'models';self.data=self.root/'mausbot-data'
        self.models.mkdir(mode=0o700);self.data.mkdir(mode=0o700)
        self.config={'host':'127.0.0.1','port':18778,'providers':{
            'ollama':{'type':'ollama','url':'http://127.0.0.1:18434','model':'synthetic-fixture'},
            'mausbot':{'type':'mausbot','url':'http://127.0.0.1:18317','bot_id':'synthetic-bot'}},
            'managed_ollama':{'binary':str(self.ollama),'port':18434,'models':str(self.models)},
            'mausbot_runtime':{'node':str(self.node),'cli':str(self.cli),'port':18317,'data_dir':str(self.data)}}

    def tearDown(self):self.tmp.cleanup()

    def save_config(self,config=None):
        stack.write_private(self.root/'config.json',json.dumps(config or self.config))

    def test_opt_in_and_loopback_command_environment(self):
        self.assertEqual(stack.managed_specs({'providers':{}}),[])
        with patch.dict(stack.os.environ,{'OLLAMA_HOST':'0.0.0.0:11434','OLLAMA_MODELS':'/unrelated'}):
            ollama,mausbot=stack.managed_specs(self.config)
        self.assertEqual(ollama['argv'],[str(self.ollama.resolve()),'serve'])
        self.assertEqual(ollama['environment']['OLLAMA_HOST'],'127.0.0.1:18434')
        self.assertEqual(ollama['environment']['OLLAMA_MODELS'],str(self.models.resolve()))
        self.assertEqual(mausbot['argv'],[str(self.node.resolve()),str(self.cli.resolve()),'serve','--local',
                                         '--no-pair','--no-open','--port','18317','--data-dir',str(self.data.resolve())])

    def test_bad_endpoint_port_and_unowned_paths_are_rejected(self):
        for url in ('http://0.0.0.0:18434','http://example.org:18434','http://127.0.0.1:18435',
                    'http://127.0.0.1:18434/api','http://user@127.0.0.1:18434'):
            config=copy.deepcopy(self.config);config['providers']['ollama']['url']=url
            with self.subTest(url=url),self.assertRaises(ValueError):stack.managed_specs(config)
        for key,value in (('port',True),('port',80),('binary','relative-binary')):
            config=copy.deepcopy(self.config);config['managed_ollama'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):stack.managed_specs(config)
        with patch.object(stack.os,'getuid',return_value=stack.os.getuid()+1),self.assertRaises(ValueError):
            stack.managed_specs(self.config)
        self.models.chmod(0o755)
        with self.assertRaises(ValueError):stack.managed_specs(self.config)

    def test_data_symlink_and_duplicate_hub_port_are_rejected(self):
        link=self.root/'model-link';link.symlink_to(self.models,target_is_directory=True)
        config=copy.deepcopy(self.config);config['managed_ollama']['models']=str(link)
        with self.assertRaises(ValueError):stack.managed_specs(config)
        config=copy.deepcopy(self.config);config['port']=18434
        with self.assertRaises(ValueError):stack.managed_specs(config)

    def test_foreign_busy_port_never_launches_or_overwrites_pid(self):
        spec=stack.managed_specs(self.config)[0]
        pid=self.root/'ollama.pid.json';stack.write_private(pid,'{"pid":1234,"match":["foreign"]}')
        before=pid.read_bytes()
        with patch.object(stack,'live',return_value=False),patch.object(stack,'port_available',return_value=False),patch.object(stack,'launch') as launch:
            with self.assertRaises(RuntimeError):stack.start_managed(self.root,[spec])
        launch.assert_not_called();self.assertEqual(pid.read_bytes(),before)

    def test_running_matching_dependency_is_reused_and_changed_settings_are_refused(self):
        spec=stack.managed_specs(self.config)[0]
        with patch.object(stack,'live',return_value=True),patch.object(stack,'port_available') as port,patch.object(stack,'launch') as launch:
            stack.start_managed(self.root,[spec])
        port.assert_not_called();launch.assert_not_called()
        with patch.object(stack,'live',side_effect=[False,True]),patch.object(stack,'launch') as launch:
            with self.assertRaises(RuntimeError):stack.start_managed(self.root,[spec])
        launch.assert_not_called()

    def test_new_dependency_uses_launch_and_ownership(self):
        spec=stack.managed_specs(self.config)[0]
        with patch.object(stack,'live',side_effect=[False,False,True]),patch.object(stack,'port_available',return_value=True),patch.object(stack,'launch') as launch,patch.object(stack.socket,'socket') as socket:
            socket.return_value.__enter__.return_value.connect_ex.return_value=0
            stack.start_managed(self.root,[spec])
        launch.assert_called_once_with(self.root,'ollama',spec['argv'],spec['environment'],ownership=spec['ownership'])
        socket.return_value.__enter__.return_value.connect_ex.assert_called_once_with(('127.0.0.1',18434))

    def test_owned_pid_tracks_process_start_and_rejects_pid_reuse(self):
        spec=stack.managed_specs(self.config)[0]
        with patch.object(stack,'live',return_value=False),patch.object(stack.subprocess,'Popen',return_value=SimpleNamespace(pid=4321)) as popen,patch.object(stack.subprocess,'check_output',return_value='fixture-start'):
            stack.launch(self.root,'ollama',spec['argv'],spec['environment'],ownership=spec['ownership'])
        saved=json.loads((self.root/'ollama.pid.json').read_text())
        self.assertEqual(saved['managed'],spec['ownership']);self.assertEqual(saved['started'],'fixture-start')
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        def inspect(argv,**kwargs):return ' '.join(spec['argv']) if argv[-1]=='command=' else 'fixture-start'
        with patch.object(stack.subprocess,'check_output',side_effect=inspect),patch.object(stack.os,'getpgid',return_value=4321):
            self.assertTrue(stack.live(self.root,'ollama',spec['ownership']))
        def reused(argv,**kwargs):return ' '.join(spec['argv']) if argv[-1]=='command=' else 'later-process'
        with patch.object(stack.subprocess,'check_output',side_effect=reused),patch.object(stack.os,'getpgid',return_value=4321):
            self.assertFalse(stack.live(self.root,'ollama',spec['ownership']))

    def test_gui_is_launched_even_if_background_startup_fails(self):
        self.save_config();order=[]
        def failed_dependencies(*args):
            order.append('dependencies');raise RuntimeError('synthetic cold startup failure')
        with patch.object(stack,'ensure_coucou',side_effect=lambda *args:order.append('gui')),patch.object(stack,'start_managed',side_effect=failed_dependencies):
            with self.assertRaisesRegex(RuntimeError,'synthetic cold'):stack.start(self.root)
        self.assertEqual(order,['gui','dependencies'])

    def test_private_configuration_required_before_managed_start(self):
        self.save_config();(self.root/'config.json').chmod(0o644)
        with patch.object(stack,'start_managed') as start,self.assertRaises(ValueError):stack.start(self.root)
        start.assert_not_called()

    def test_dependencies_start_before_hub_and_stop_after_hub(self):
        self.save_config();order=[]
        with patch.object(stack,'start_managed',side_effect=lambda root,specs:order.append('dependencies')),patch.object(stack,'live',return_value=False),patch.object(stack,'hub_request',side_effect=[OSError(),{'ok':True}]),patch.object(stack,'launch',side_effect=lambda root,name,*args,**kwargs:order.append(name)),patch.object(stack.sys,'platform','linux'),patch('sys.stdout',new=io.StringIO()):
            stack.start(self.root)
        self.assertEqual(order,['dependencies','hub'])
        stopped=[]
        with patch.object(stack,'stop_process',side_effect=lambda root,name,ownership=None:stopped.append((name,ownership))):
            stack.stop(self.root)
        names=[name for name,_ in stopped]
        self.assertLess(names.index('hub'),names.index('mausbot'));self.assertLess(names.index('hub'),names.index('ollama'))
        self.assertEqual(stopped[-1][1],stack.managed_specs(self.config)[0]['ownership'])

    def test_stop_never_signals_nonmatching_process(self):
        spec=stack.managed_specs(self.config)[0]
        with patch.object(stack,'live',return_value=False),patch.object(stack.os,'killpg') as kill:
            stack.stop_process(self.root,'ollama',spec['ownership'])
        kill.assert_not_called()

    def test_new_coucou_launch_prefers_auto_when_configured(self):
        for auto,expected in ((True,'assistant-auto'),(False,'assistant-ollama')):
            config={'host':'127.0.0.1','port':18778,'providers':{'auto':{'type':'routing'}} if auto else {},
                    'coucou_capability':'synthetic-capability','coucou_app':str(self.root/'Coucou.app')}
            executable=self.root/'Coucou.app/Contents/MacOS/Coucou'
            executable.parent.mkdir(parents=True,exist_ok=True);executable.write_text('fixture')
            self.save_config(config)
            with self.subTest(auto=auto),patch.object(stack,'live',return_value=False),patch.object(stack,'hub_request',side_effect=[OSError(),{'ok':True}]),patch.object(stack,'launch'),patch.object(stack,'launch_coucou'),patch.object(stack.sys,'platform','darwin'),patch.object(stack.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout=b'')) as run,patch('sys.stdout',new=io.StringIO()):
                stack.start(self.root)
            writes=[call.args[0] for call in run.call_args_list if call.args[0][0]=='defaults']
            self.assertTrue(any(argv[-1]==expected for argv in writes))


if __name__=='__main__':unittest.main()
