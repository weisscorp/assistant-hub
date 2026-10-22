"""Explicit local recording, imported as separate mic/system corpus tracks."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
import wave
from home_link import encoded
from recording_archive import prepare,atomic_text
from audio_meter import tail_levels

class DesktopSpeechCapture:
    def __init__(self,app):
        self.app=app;self.store=app.mobile.speech
        self.root=app.store.directory/'speech-capture';self.root.mkdir(mode=0o700,exist_ok=True)
        self.helper=Path(app.config.get('speech_capture_helper',str(Path.home()/'.local/share/assistant-stack/runtime/SpeechDatasetCapture.app/Contents/MacOS/SpeechCapture')))
        self.archive=Path(app.config.get('recordings_directory',str(Path.home()/'Documents/Аудиозаписи')));self.archive.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.owned_at=0;self.owned_value=False;self.sample_count_at=0;self.sample_count=0;self.finalizers={}
        self.lock=threading.RLock();self.state_path=self.root/'capture.json'
        self.state=json.loads(self.state_path.read_text()) if self.state_path.exists() else {'phase':'idle','error':''}
        self.permission_probe_at=0;self.transcriber=None;self.process=None;self.watcher=None;self.watcher_session=None
        obsolete=self.state.get('permissionPane')=='screenCapture' or 'отклонил' in self.state.get('error','')
        if obsolete and not self._owned():
            self.state['phase']='idle';self.state['error']='';self.state['permissionPane']=None;self._save()
        elif self.state.get('session'):
            directory=self.root/self.state['session']
            if directory.is_dir():
                prepare(directory,self.archive,self.state['session'],self.state.get('started'),self.state.get('sources'))
                if self._owned() and self.state.get('transcribe'):
                    self._start_transcriber(directory,self.state['session']);self._save()
            self._watch()
    def _save(self):
        temporary=self.state_path.with_suffix('.tmp');temporary.write_text(json.dumps(self.state,ensure_ascii=False));temporary.chmod(0o600);os.replace(temporary,self.state_path)
    def _owned(self):
        now=time.monotonic()
        if now-self.owned_at<.3:return self.owned_value
        self.owned_at=now;self.owned_value=self._owned_uncached();return self.owned_value
    def _owned_uncached(self):
        pid=self.state.get('pid')
        if not isinstance(pid,int) or pid<=0:return False
        if self.process is not None and self.process.pid==pid and self.process.poll() is not None:return False
        result=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True)
        return result.returncode==0 and result.stdout.strip()==self.state.get('command')
    def _permission_status(self):
        path=self.root/'permissions.json';value={}
        if path.is_file() and (not self.helper.is_file() or path.stat().st_mtime>=self.helper.stat().st_mtime):
            try:value=json.loads(path.read_text())
            except ValueError:pass
        now=time.time()
        if sys.platform=='darwin' and now-self.permission_probe_at>3 and now-value.get('checkedAt',0)>3:
            self.permission_probe_at=now
            bundle=self.helper.parents[2]
            if bundle.suffix=='.app' and (bundle/'Contents/Info.plist').is_file():
                # Fresh independent app process: avoids a stale TCC result in the old recorder.
                # This mode only queries public APIs, never requests permission or captures audio.
                threading.Thread(target=lambda:subprocess.run(['open','-n','-a',str(bundle),'--args','--permission-status',str(path)],capture_output=True),daemon=True).start()
        return value
    def _refresh_permission_failure(self,alive):
        pane=self.state.get('permissionPane') or ('screenCapture' if 'отклонил' in self.state.get('error','') else None)
        if pane and not alive:
            permissions=self._permission_status()
            if time.time()-permissions.get('checkedAt',0)<10 and permissions.get(pane) is True:
                self.state['error']='';self.state['permissionPane']=None;self.state['phase']='idle';self._save()
                return None
        return pane
    def status(self):
        with self.lock:
            alive=self._owned()
            permission_pane=self._refresh_permission_failure(alive)
            transcript=self.root/self.state.get('session','')/'live-transcripts.jsonl'
            import sys
            tools=str(Path(__file__).resolve().parents[1]/'tools')
            if tools not in sys.path:sys.path.insert(0,tools)
            from speech_live import transcript_preview
            preview=transcript_preview(transcript)
            directory=transcript.parent
            partial=directory/'partial-preview.json';partial_value={}
            if partial.is_file():
                try:partial_value=json.loads(partial.read_text())
                except ValueError:pass
                if time.time()-partial_value.get('capturedAt',0)<30:preview=partial_value.get('text','')
            job=directory/'transcription-state.json';job_value={}
            if job.is_file():
                try:job_value=json.loads(job.read_text())
                except ValueError:pass
            archive_meta=directory/'archive.json';archive_value={}
            if archive_meta.is_file():
                try:archive_value=json.loads(archive_meta.read_text())
                except ValueError:pass
            if time.monotonic()-self.sample_count_at>2:
                self.sample_count=self.store.list()['total'];self.sample_count_at=time.monotonic()
            return {'available':sys.platform=='darwin' and self.helper.is_file(),'alive':alive,
                    'archiveDirectory':str(self.archive),'archivePhase':archive_value.get('phase'),'archiveError':archive_value.get('error',''),'transcriptionPhase':job_value.get('phase'),'transcriptionError':job_value.get('error',''),'transcriptAgeSeconds':int(time.time()-partial_value['capturedAt']) if partial_value.get('capturedAt') else None,'permissionPane':permission_pane,'transcriptPreview':preview,'transcribing':self.transcriber is not None and self.transcriber.poll() is None,'recording':alive and self.state.get('phase')=='recording','phase':self.state.get('phase','idle'),
                    'session':self.state.get('session'),'sources':self.state.get('sources',[]),'error':self.state.get('error',''),
                    'durationSeconds':int(time.time()-self.state.get('started',time.time())) if alive else 0,
                    'chunks':self.state.get('chunks',0),'levels':self.state.get('levels',[0]*32),'levelsByTrack':self.state.get('levelsByTrack',{}),'meterUpdatedAt':self.state.get('meterUpdatedAt',{}),'rawDirectory':str(self.root/self.state['session']) if self.state.get('session') else None,'samples':self.sample_count}
    def start(self,sources,transcribe=True):
        if type(transcribe) is not bool:raise ValueError("Invalid transcription flag")
        if not isinstance(sources,list) or not sources or len(set(sources))!=len(sources) or not set(sources)<= {'mic','system'}:raise ValueError('Choose mic and/or system')
        with self.lock:
            if self._owned():raise ValueError('Recording already active')
            if sys.platform!='darwin' or not self.helper.is_file():raise ValueError('Build the macOS capture helper first')
            session=str(uuid.uuid4());directory=self.root/session;directory.mkdir(mode=0o700)
            argv=[str(self.helper),str(directory),','.join(sorted(sources))]
            pid=self._launch(argv,directory)
            self.state={'pid':pid,'command':' '.join(argv),'session':session,'sources':sources,
                        'transcribe':transcribe,'started':time.time(),'phase':'starting','error':'','chunks':0}
            self.owned_at=0;prepare(directory,self.archive,session,self.state['started'],sources)
            self._save();self._start_transcriber(directory,session);self._watch();return self.status()
    def _launch(self,argv,directory):
        bundle=self.helper.parents[2]
        log=directory/'capture.jsonl';log.touch(mode=0o600)
        if bundle.suffix=='.app' and (bundle/'Contents/Info.plist').is_file():
            # LaunchServices makes the helper an independent GUI app for TCC attribution.
            # A child of the Python Hub may otherwise inherit the host's permission identity.
            subprocess.run(['open','-n','-a',str(bundle),'--stdout',str(log),'--stderr',str(directory/'capture-stderr.log'),'--args',*argv[1:]],check=True,capture_output=True)
            self.process=None
            until=time.monotonic()+3
            while time.monotonic()<until:
                processes=subprocess.run(['ps','-axo','pid=,command='],capture_output=True,text=True)
                for line in processes.stdout.splitlines():
                    parts=line.strip().split(None,1)
                    if len(parts)==2 and parts[1]==' '.join(argv):return int(parts[0])
                if '"error"' in log.read_text(errors='replace'):return None
                time.sleep(.05)
            raise ValueError('Не удалось запустить приложение захвата')
        with log.open('ab') as stream:
            self.process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,start_new_session=True)
        return self.process.pid
    def stop(self):
        with self.lock:
            if self._owned():os.kill(self.state['pid'],signal.SIGTERM);self.state['phase']='stopping';self._save()
            return self.status()
    def _watch(self):
        if self.watcher and self.watcher.is_alive() and self.watcher_session==self.state.get("session"):return
        self.watcher_session=self.state.get("session")
        self.watcher=threading.Thread(target=self._monitor,daemon=True);self.watcher.start()
    def import_wav(self,path,session,track):
        data=Path(path).read_bytes();sha=hashlib.sha256(data).hexdigest()
        self.store.audio.blob(sha,data)
        with wave.open(str(path)) as recording:duration=recording.getnframes()*1000//16000
        identifier=str(uuid.uuid5(uuid.UUID(session),Path(path).name))
        value={'id':identifier,'sessionId':session,'createdAt':int(time.time()*1000),'durationMs':duration,
               'engine':'recording','recognized':'','corrected':'','reviewed':False,'deleted':False,'sampleRate':16000,
               'origin':'desktop-'+track,'captureSource':1 if track=='mic' else 2,'deviceType':0,
               'rawSha256':sha,'cleanSha256':sha,'revision':1}
        old=next((r for r in self.store.rows() if r['id']==identifier),None)
        if old:return old
        return self.store.put(self.app.device,value)
    def _start_transcriber(self,directory,session):
        if not self.state.get('transcribe') or self.state.get('asrStarted'):return
        python=Path(self.app.config.get('speech_teacher_python',str(Path.home()/'.local/share/assistant-stack/runtime/speech-venv/bin/python')))
        if not python.is_file():return
        script=Path(__file__).resolve().parents[1]/'tools/speech_live.py'
        with (directory/'transcription.log').open('ab') as log:
            self.transcriber=subprocess.Popen([str(python),str(script),'--state',str(self.store.directory),'--session',session,'--output',str(directory/'live-transcripts.jsonl'),'--capture-state',str(self.state_path),'--directory',str(directory)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        self.state['asrStarted']=True
    def finalize(self,session,directory=None):
        directory=Path(directory or self.root/session)
        if not list(directory.glob('*.raw.caf')):return
        prepare(directory,self.archive,session)
        metadata=json.loads((directory/'archive.json').read_text())
        if metadata.get('phase')=='ready':return
        owner=directory/'finalizer.json'
        if owner.is_file():
            try:
                previous=json.loads(owner.read_text())
                active=subprocess.run(['ps','-p',str(previous['pid']),'-o','command='],capture_output=True,text=True)
                if active.returncode==0 and active.stdout.strip()==previous['command']:return
            except (OSError,ValueError,KeyError):pass
        process=self.finalizers.get(session)
        if process and process.poll() is None:return
        python=Path(self.app.config.get('speech_teacher_python',str(Path.home()/'.local/share/assistant-stack/runtime/speech-venv/bin/python')))
        if not python.is_file():return
        script=Path(__file__).resolve().parents[1]/'tools/recording_finish.py'
        with (directory/'finalize.log').open('ab') as log:
            argv=[str(python),str(script),'--directory',str(directory)]
            self.finalizers[session]=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        atomic_text(owner,json.dumps({'pid':self.finalizers[session].pid,'command':' '.join(argv)}))
    def finalize_history(self):
        # Sequential jobs: preserve every original and avoid loading many ASR models at once.
        def work():
            for directory in sorted(self.root.iterdir()):
                if directory.is_dir() and list(directory.glob('*.raw.caf')) and directory.name!=self.state.get('session'):
                    self.finalize(directory.name,directory)
                    process=self.finalizers.get(directory.name)
                    if process:process.wait()
        threading.Thread(target=work,daemon=True).start()
    def _monitor(self):
        session=self.state['session'];directory=self.root/session;position=0;pending='';meters={};last_save=0;last_phase=''
        while True:
            with self.lock:
                if self.state.get('session')!=session:return
                alive=self._owned()
                log=directory/'capture.jsonl'
                if log.exists():
                    with log.open() as stream:
                        stream.seek(position);pending+=stream.read();position=stream.tell()
                    lines=pending.split('\n');pending=lines.pop()
                    for line in lines:
                        try:event=json.loads(line)
                        except (ValueError,TypeError):continue
                        if event.get('event')=='meter':
                            levels=event.get('levels',[])
                            if len(levels)==32 and all(type(x) in (float,int) and 0<=x<=1 for x in levels):
                                meters[event.get('track')]=levels
                                self.state['levelsByTrack']=dict(meters)
                                self.state.setdefault('meterUpdatedAt',{})[event.get('track')]=time.time()
                                self.state['levels']=[max(track[i] for track in meters.values()) for i in range(32)]
                        elif event.get('event')=='started' and self.state['phase']=='starting':self.state['phase']='recording'
                        elif event.get('event')=='raw':
                            raw=Path(event.get('path',''))
                            if raw.parent==directory and raw.is_file() and not raw.with_suffix('.json').exists():
                                metadata={**event,'sessionId':session,'sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'processing':'none','capturedAt':int(time.time()*1000)}
                                raw.with_suffix('.json').write_text(json.dumps(metadata));raw.with_suffix('.json').chmod(0o600)
                        elif event.get('event')=='error':self.state['error']=event.get('message','Capture failed');self.state['permissionPane']=event.get('permissionPane');self.state['phase']='failed'
                if self.state.get('phase')=='recording':
                    for track in self.state.get('sources',[]):
                        paths=sorted(directory.glob(track+'-*.part.wav'))
                        if paths:
                            try:
                                measured=tail_levels(paths[-1])
                                if measured:
                                    self.state.setdefault('levelsByTrack',{})[track]=measured
                                    self.state.setdefault('meterUpdatedAt',{})[track]=paths[-1].stat().st_mtime
                            except OSError:pass
                    for track,updated in self.state.get('meterUpdatedAt',{}).items():
                        if time.time()-updated>1:self.state.setdefault('levelsByTrack',{})[track]=[0]*32
                if self.app.store.directory.stat().st_uid!=os.getuid():return
                for path in sorted(directory.glob('*.ready.wav')):
                    if (directory/(path.name+'.imported')).exists():continue
                    try:
                        self.import_wav(path,session,path.name.split('-')[0])
                        (directory/(path.name+'.imported')).touch(mode=0o600)
                        self.state['chunks']+=1
                        self._start_transcriber(directory,session)
                        path.unlink() # Canonical corpus audio now exists in the separate ASR store.
                    except Exception as error:self.state['error']='Не удалось сохранить часть: '+str(error)[:300]
                if not alive:
                    if self.process is not None and self.process.returncode not in (None,0) and not self.state.get('error'):
                        self.state['error']=log.read_text(errors='replace')[-1000:] if log.exists() else 'Захват завершился с ошибкой'
                    self.state['phase']='failed' if self.state.get('error') else 'stopped';self._save();self.finalize(session,directory);return
                if time.monotonic()-last_save>1 or last_phase!=self.state['phase']:
                    self._save();last_save=time.monotonic();last_phase=self.state['phase']
            time.sleep(.05)
