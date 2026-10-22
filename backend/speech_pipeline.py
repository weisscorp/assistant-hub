"""Explicit background teacher/training jobs. Never starts recording or installs a model on a phone."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

class SpeechPipeline:
    def __init__(self,app):
        self.app=app;self.root=app.store.directory/'speech-jobs';self.root.mkdir(mode=0o700,exist_ok=True)
        self.python=Path(app.config.get('speech_teacher_python',str(Path.home()/'.local/share/assistant-stack/runtime/speech-venv/bin/python')))
        self.training_python=Path(app.config.get('speech_training_python',str(Path.home()/'.local/share/assistant-stack/runtime/speech-training-venv/bin/python')))
        self.tools=Path(__file__).resolve().parents[1]/'tools'
        self.lock=threading.RLock();self.path=self.root/'status.json'
        self.state=json.loads(self.path.read_text()) if self.path.exists() else {'phase':'idle','teacherDataset':None}
        self.process=None;self.watcher=None;self.watcher_pid=None
        if self.state.get('phase')=='running':self._watch()
    def _save(self):
        tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps(self.state));tmp.chmod(0o600);os.replace(tmp,self.path)
    def _alive(self):
        pid=self.state.get('pid')
        if not isinstance(pid,int):return False
        if self.process is not None and self.process.pid==pid and self.process.poll() is not None:return False
        p=subprocess.run(['ps','-p',str(pid),'-o','command='],text=True,capture_output=True)
        return p.returncode==0 and p.stdout.strip()==self.state.get('command')
    def status(self):
        with self.lock:
            value={k:v for k,v in self.state.items() if k not in ('command','pid','log')}
            lib=self.python.parent.parent/'lib'
            value['teacherAvailable']=self.python.exists() and any(lib.glob('python*/site-packages/faster_whisper/__init__.py'))
            value['trainingAvailable']=self.training_python.exists() and any((self.training_python.parent.parent/'lib').glob('python*/site-packages/tone/training/model_wrapper.py'))
            value['running']=self._alive()
            dataset=self.state.get('teacherDataset')
            if dataset and (Path(dataset)/'report.json').exists():value['teacherReport']=json.loads((Path(dataset)/'report.json').read_text())
            return value
    def start(self,action):
        if action not in ('teacher','train'):raise ValueError('Choose teacher or train')
        with self.lock:
            if self._alive():raise ValueError('Speech job already running')
            directory=self.root/str(uuid.uuid4());directory.mkdir(mode=0o700)
            if action=='teacher':
                if not self.status()['teacherAvailable']:raise ValueError('Teacher runtime not installed')
                if not any(not r['deleted'] for r in self.app.mobile.speech.rows()):raise ValueError('Сначала запишите диктовку или встречу')
                output=directory/'dataset'
                argv=[str(self.python),str(self.tools/'speech_teacher.py'),'--state',str(self.app.mobile.speech.directory),'--output',str(output)]
            else:
                if not self.status()['trainingAvailable']:raise ValueError('Training runtime not installed')
                dataset=self.state.get('teacherDataset')
                if not dataset or not (Path(dataset)/'report.json').is_file():raise ValueError('Сначала выполните разметку')
                report=json.loads((Path(dataset)/'report.json').read_text())
                if not report['readyToTrain']:raise ValueError('Нужны фразы как минимум из двух разных сессий для обучения и проверки')
                output=directory/'model'
                argv=[str(self.training_python),str(self.tools/'train_speech_model.py'),'--dataset',dataset,'--output',str(output),'--allow-pseudo-labels']
            log=directory/'job.log'
            with log.open('wb') as stream:process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,start_new_session=True)
            self.process=process
            previous=self.state.get('teacherDataset')
            self.state={'phase':'running','action':action,'pid':process.pid,'command':' '.join(argv),'log':str(log),
                        'startedAt':time.time(),'output':str(output),'teacherDataset':previous,'error':''}
            self._save();self._watch();return self.status()
    def _watch(self):
        if self.watcher and self.watcher.is_alive() and self.watcher_pid==self.state.get("pid"):return
        self.watcher_pid=self.state.get("pid")
        self.watcher=threading.Thread(target=self._monitor,daemon=True);self.watcher.start()
    def _monitor(self):
        pid=self.state.get('pid')
        while True:
            with self.lock:
                if self.state.get('pid')!=pid:return
                if not self._alive():
                    output=Path(self.state['output']);artifact=output/('report.json' if self.state['action']=='teacher' else 'comparison.json')
                    if artifact.is_file():
                        self.state['phase']='completed';self.state['report']=json.loads(artifact.read_text())
                        if self.state['action']=='teacher':self.state['teacherDataset']=str(output)
                    else:
                        self.state['phase']='failed';log=Path(self.state['log'])
                        self.state['error']=log.read_text(errors='replace')[-2000:] if log.exists() else 'Job ended without a result'
                    self._save();return
            time.sleep(1)
