import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import wave
from types import SimpleNamespace
from speech_dataset import SpeechDataset

class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=SpeechDataset(Path(self.tmp.name)/'state');self.device=str(uuid.uuid4())
    def sample(self,session=None,frequency=10):
        import math,struct
        stream=io.BytesIO()
        with wave.open(stream,'wb') as audio:
            audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
            audio.writeframes(b''.join(struct.pack('<h',int(1000*math.sin(i*frequency/100))) for i in range(32000)))
        data=stream.getvalue();sha=hashlib.sha256(data).hexdigest();self.store.audio.blob(sha,data)
        return {'id':str(uuid.uuid4()),'sessionId':session or str(uuid.uuid4()),'createdAt':1,'durationMs':2000,
                'engine':'recording','recognized':'','corrected':'','reviewed':False,'deleted':False,'sampleRate':16000,
                'origin':'phone','captureSource':1,'deviceType':0,'rawSha256':sha,'cleanSha256':sha,'revision':1}
    def test_unreviewed_audio_is_not_human_ground_truth_and_deletion_propagates(self):
        sample=self.sample();self.store.put(self.device,sample)
        self.assertEqual(0,self.store.export(Path(self.tmp.name)/'empty')['exported'])
        sample={**sample,'reviewed':True,'corrected':'Синтетическая речь','revision':2};self.store.put(self.device,sample)
        self.assertEqual(1,self.store.export(Path(self.tmp.name)/'reviewed')['exported'])
        sample={**sample,'deleted':True,'reviewed':False,'corrected':'','revision':3};self.store.put(self.device,sample)
        self.assertEqual(0,self.store.list()['total'])
        with self.assertRaises(KeyError):self.store.audio.blob(sample['rawSha256'])
    def test_same_session_and_duplicate_audio_do_not_leak_between_splits(self):
        a=self.sample(frequency=11);b=self.sample(session=a['sessionId'],frequency=21);c=self.sample(frequency=32)
        for item in (a,b,c):self.store.put(self.device,{**item,'reviewed':True,'corrected':'Тестовая фраза'})
        folder=Path(self.tmp.name)/'export';report=self.store.export(folder)
        self.assertTrue(report['readyToTrain'])
        partitions={name:[json.loads(line) for line in (folder/(name+'.jsonl')).read_text().splitlines()] for name in ('train','validation')}
        self.assertFalse({r['sessionId'] for r in partitions['train']} & {r['sessionId'] for r in partitions['validation']})
    def test_teacher_filters_noise_cuts_real_audio_and_marks_pseudo_labels(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from speech_teacher import run
        for freq in (10,20):self.store.put(self.device,self.sample(frequency=freq))
        good=SimpleNamespace(start=.1,end=1.4,text='Синтетическая фраза',avg_logprob=-.1,no_speech_prob=.01,compression_ratio=1)
        bad=SimpleNamespace(start=0,end=1.5,text='галлюцинация',avg_logprob=-2,no_speech_prob=.9,compression_ratio=4)
        teacher=SimpleNamespace(transcribe=lambda *a,**kw:([good,bad],None))
        folder=Path(self.tmp.name)/'teacher';report=run(self.store,teacher,folder,{'phone'})
        self.assertEqual(2,report['clips']);self.assertEqual(2,report['filtered']);self.assertTrue(report['readyToTrain'])
        for name in ('train','validation'):
            row=json.loads((folder/(name+'.jsonl')).read_text().splitlines()[0]);self.assertFalse(row['humanReviewed'])
            with wave.open(row['audio']) as clip:self.assertGreater(clip.getnframes(),16000)
    def test_desktop_import_is_idempotent_and_keeps_tracks_separate(self):
        from desktop_speech_capture import DesktopSpeechCapture
        app=SimpleNamespace(store=SimpleNamespace(directory=Path(self.tmp.name)/'app'),mobile=SimpleNamespace(speech=self.store),config={},device=self.device)
        app.store.directory.mkdir(mode=0o700)
        capture=DesktopSpeechCapture(app);sample=self.sample()
        file=Path(self.tmp.name)/'mic-000001.ready.wav';file.write_bytes(self.store.audio.blob(sample['rawSha256']))
        session=str(uuid.uuid4());value=capture.import_wav(file,session,'mic')
        self.assertEqual(value,capture.import_wav(file,session,'mic'));self.assertEqual('desktop-mic',value['origin'])
        self.assertFalse(capture.status()['recording'])

    def test_live_preview_is_idempotent_and_never_changes_source_or_human_labels(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from speech_live import transcribe_pending
        sample=self.sample();self.store.put(self.device,sample)
        original=self.store.audio.blob(sample['rawSha256'])
        model=SimpleNamespace(transcribe=lambda *a,**kw:([SimpleNamespace(text=' Синтетическая фраза ')],None))
        output=Path(self.tmp.name)/'live.jsonl'
        self.assertEqual(1,transcribe_pending(self.store,model,sample['sessionId'],output))
        self.assertEqual(0,transcribe_pending(self.store,model,sample['sessionId'],output))
        self.assertEqual(original,self.store.audio.blob(sample['rawSha256']))
        self.assertFalse(self.store.rows()[0]['reviewed']);self.assertFalse(json.loads(output.read_text())['humanReviewed'])

    def test_transcript_preview_uses_audio_times_and_keeps_concurrent_tracks(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
        from speech_live import transcript_preview
        path=Path(self.tmp.name)/'preview.jsonl'
        rows=[{'origin':'desktop-mic','endMs':20000,'words':[{'text':'старая фраза','startMs':1000,'endMs':4000},{'text':'мой голос','startMs':16000,'endMs':18000}]},
              {'origin':'desktop-system','endMs':21000,'words':[{'text':'голос встречи','startMs':17000,'endMs':20000}]}]
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        preview=transcript_preview(path)
        self.assertNotIn('старая фраза',preview);self.assertIn('мой голос',preview);self.assertIn('голос встречи',preview)
        rows.append({'origin':'desktop-mic','endMs':45000,'words':[{'text':'новая фраза','startMs':40000,'endMs':43000}]})
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.assertEqual('новая фраза',transcript_preview(path))

    def test_bundled_capture_uses_launchservices_and_identifies_only_exact_process(self):
        from desktop_speech_capture import DesktopSpeechCapture
        from unittest.mock import patch
        root=Path(self.tmp.name)
        bundle=root/'Capture.app';helper=bundle/'Contents/MacOS/Capture';helper.parent.mkdir(parents=True)
        helper.touch();(bundle/'Contents/Info.plist').write_text('synthetic')
        app=SimpleNamespace(store=SimpleNamespace(directory=root/'app'),mobile=SimpleNamespace(speech=self.store),config={'speech_capture_helper':str(helper)},device=self.device)
        app.store.directory.mkdir(mode=0o700)
        capture=DesktopSpeechCapture(app);directory=root/'session';directory.mkdir()
        argv=[str(helper),str(directory),'mic,system']
        def fake_run(command,**kwargs):
            if command[0]=='open':return SimpleNamespace(returncode=0,stdout='',stderr='')
            return SimpleNamespace(returncode=0,stdout='12 unrelated '+str(helper)+'\n42 '+' '.join(argv)+'\n',stderr='')
        with patch('desktop_speech_capture.subprocess.run',side_effect=fake_run) as run:
            self.assertEqual(42,capture._launch(argv,directory))
            self.assertEqual(['open','-n','-a',str(bundle)],run.call_args_list[0].args[0][:4])
            self.assertIn('--args',run.call_args_list[0].args[0])
        self.assertIsNone(capture.process)

    def test_permission_failure_clears_after_live_grant_without_starting_recording(self):
        import time
        from unittest.mock import patch
        from desktop_speech_capture import DesktopSpeechCapture
        app=SimpleNamespace(store=SimpleNamespace(directory=Path(self.tmp.name)/'app'),mobile=SimpleNamespace(speech=self.store),config={},device=self.device)
        app.store.directory.mkdir(mode=0o700);capture=DesktopSpeechCapture(app)
        capture.state={'phase':'failed','error':'Permission denied','permissionPane':'screenCapture'}
        with patch.object(capture,'_permission_status',return_value={'screenCapture':False,'checkedAt':time.time()}):
            self.assertEqual('screenCapture',capture.status()['permissionPane'])
        with patch.object(capture,'_permission_status',return_value={'screenCapture':True,'checkedAt':time.time()}):
            value=capture.status()
            self.assertIsNone(value['permissionPane']);self.assertEqual('',value['error']);self.assertFalse(value['recording'])
        capture.state={'phase':'failed','error':'Disk full'}
        with patch.object(capture,'_permission_status') as probe:
            self.assertEqual('Disk full',capture.status()['error']);probe.assert_not_called()

    def test_audio_only_migration_drops_old_screen_failure_and_does_not_replay_it(self):
        from desktop_speech_capture import DesktopSpeechCapture
        root=Path(self.tmp.name)/'app';root.mkdir(mode=0o700)
        capture=root/'speech-capture';capture.mkdir()
        session=str(uuid.uuid4());folder=capture/session;folder.mkdir()
        (capture/'capture.json').write_text(json.dumps({'session':session,'phase':'failed','error':'Пользователь отклонил захват','permissionPane':'screenCapture'}))
        (folder/'capture.jsonl').write_text(json.dumps({'event':'error','message':'Пользователь отклонил захват'})+'\n')
        app=SimpleNamespace(store=SimpleNamespace(directory=root),mobile=SimpleNamespace(speech=self.store),config={},device=self.device)
        recorder=DesktopSpeechCapture(app);state=recorder.status()
        self.assertEqual('idle',state['phase']);self.assertEqual('',state['error']);self.assertIsNone(state['permissionPane']);self.assertFalse(state['recording'])
        self.assertIsNone(recorder.watcher)

if __name__=='__main__':unittest.main()

class CorpusHttpTests(unittest.TestCase):
    def test_authenticated_sync_and_recording_controls_do_not_capture_on_read(self):
        import urllib.request,urllib.error
        from test_hub_stack import HubTests
        fixture=HubTests();fixture.setUp()
        data=DatasetTests();data.setUp()
        try:
            self.assertFalse(fixture.request('/api/speech-capture')['recording'])
            fixture.app.config['coucou_capability']='synthetic-coucou'
            scoped='/coucou/synthetic-coucou/api/speech-capture'
            self.assertFalse(fixture.request(scoped,auth=False,headers={'Authorization':'Bearer ollama'})['recording'])
            with self.assertRaises(urllib.error.HTTPError):fixture.request(scoped,auth=False)
            with self.assertRaises(urllib.error.HTTPError):fixture.request('/coucou/synthetic-coucou/api/speech-samples',auth=False,headers={'Authorization':'Bearer ollama'})
            self.assertFalse(fixture.request('/api/speech-pipeline')['running'])
            for path,body in [('/api/speech-capture',{'action':'start','sources':[]}),('/api/speech-pipeline',{'action':'teacher'})]:
                with self.assertRaises(urllib.error.HTTPError) as rejected:fixture.request(path,body)
                self.assertEqual(400,rejected.exception.code)
            sample=data.sample();audio=data.store.audio.blob(sample['rawSha256'])
            request=urllib.request.Request(fixture.url+'/link/'+data.device+'/speech-audio/'+sample['rawSha256'],data=audio,headers={'Authorization':'Bearer '+fixture.token})
            with urllib.request.urlopen(request) as response:self.assertEqual(200,response.status)
            self.assertEqual(sample,fixture.request('/link/'+data.device+'/speech-samples',sample))
            self.assertEqual(1,fixture.request('/link/'+data.device+'/speech-samples?offset=0')['total'])
            with self.assertRaises(urllib.error.HTTPError) as denied:fixture.request('/api/speech-samples',auth=False)
            self.assertEqual(401,denied.exception.code)
        finally:data.doCleanups();fixture.tearDown()
