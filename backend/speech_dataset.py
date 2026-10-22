"""Personal ASR corpus, kept separate from assistant memory and ordinary notes."""
from contextlib import contextmanager
import argparse
import hashlib
import json
import os
import sqlite3
import shutil
from pathlib import Path
from home_link import identifier, integer, text, encoded
from audio_notes import AudioNotes

FIELDS={'id','sessionId','createdAt','durationMs','engine','recognized','corrected','reviewed','deleted',
        'origin','captureSource','deviceType','sampleRate','rawSha256','cleanSha256','revision'}

class SpeechDataset:
    def __init__(self,directory):
        self.directory=Path(directory)
        self.audio=AudioNotes(self.directory)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY, device TEXT NOT NULL, data TEXT NOT NULL)')
        os.chmod(self.directory/'samples.sqlite',0o600)
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.directory/'samples.sqlite',timeout=30)
        try:
            with db:yield db
        finally:db.close()
    def put(self,device,value):
        identifier(device)
        if not isinstance(value,dict) or set(value)!=FIELDS:raise ValueError('invalid speech sample')
        identifier(value['id']);identifier(value['sessionId'])
        for key in ('createdAt','revision'):integer(value[key],1)
        integer(value['durationMs'],100,600000)
        if value['origin'] not in ('phone','desktop-mic','desktop-system'):raise ValueError('invalid capture origin')
        for key in ('captureSource','deviceType'):integer(value[key],0,1024)
        if value['sampleRate']!=16000 or value['engine'] not in ('vosk','t-one','recording'):raise ValueError('invalid sample format/engine')
        for key in ('recognized','corrected'):text(value[key],4000,True)
        for key in ('reviewed','deleted'):
            if type(value[key]) is not bool:raise ValueError('invalid sample flag')
        if value['reviewed'] and (not value['corrected'].strip() or value['deleted']):raise ValueError('reviewed speech needs a human transcript')
        for key in ('rawSha256','cleanSha256'):
            sha=value[key]
            if not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('invalid audio digest')
            if not value['deleted']:
                import wave,io
                with wave.open(io.BytesIO(self.audio.blob(sha))) as recording:
                    if recording.getnframes()*1000//16000!=value['durationMs']:raise ValueError('sample duration mismatch')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT device,data FROM samples WHERE id=?',(value['id'],)).fetchone()
            if row:
                previous=json.loads(row[1])
                if row[0]!=device:raise ValueError('sample belongs to another device')
                if any(previous[k]!=value[k] for k in ('sessionId','createdAt','durationMs','rawSha256','cleanSha256','recognized','engine','sampleRate','origin','captureSource','deviceType')):raise ValueError('original sample is immutable')
                if previous['revision']>value['revision']:return previous
                if previous['revision']==value['revision'] and previous!=value:raise ValueError('sample revision conflict')
            db.execute('INSERT OR REPLACE INTO samples VALUES(?,?,?)',(value['id'],device,encoded(value).decode()))
        if value['deleted']:
            referenced={r[k] for r in self.rows() if not r['deleted'] for k in ('rawSha256','cleanSha256')}
            for sha in (value['rawSha256'],value['cleanSha256']):
                if sha not in referenced:(self.audio.blobs/(sha+'.wav')).unlink(missing_ok=True)
        return value
    def rows(self):
        with self.connect() as db:return [json.loads(r[0]) for r in db.execute('SELECT data FROM samples ORDER BY id')]
    def list(self,offset=0,limit=10):
        integer(offset);integer(limit,1,10)
        rows=[r for r in self.rows() if not r['deleted']]
        return {'items':rows[offset:offset+limit],'next':offset+limit if len(rows)>offset+limit else None,
                'total':len(rows),'reviewed':sum(r['reviewed'] for r in rows)}
    def export(self,destination,clean=False):
        destination=Path(destination)
        if destination.exists() and any(destination.iterdir()):raise ValueError('export destination must be empty')
        destination.mkdir(parents=True,exist_ok=True,mode=0o700)
        audio_dir=destination/'audio';audio_dir.mkdir(mode=0o700)
        # Same session, and identical recording bytes, always stay in the same split.
        rows=[r for r in self.rows() if r['reviewed'] and not r['deleted']]
        groups={r['sessionId']:r['sessionId'] for r in rows}
        def find(key):
            while groups[key]!=key:key=groups[key]
            return key
        hashes={}
        for r in rows:
            key=r['cleanSha256' if clean else 'rawSha256']
            if key in hashes:groups[find(r['sessionId'])]=find(hashes[key])
            else:hashes[key]=r['sessionId']
        session_groups=sorted({find(r['sessionId']) for r in rows})
        # Preserve complete sessions for validation; never silently mix repeated utterances.
        validation=set(session_groups[-max(1,len(session_groups)//5):]) if len(session_groups)>1 else set()
        output={'train':[],'validation':[]};seen=set()
        for r in rows:
            sha=r['cleanSha256' if clean else 'rawSha256']
            if sha in seen:continue
            seen.add(sha)
            source=self.audio.blobs/(sha+'.wav')
            # Read through validated blob access. Corrupt/missing audio is an export failure.
            data=self.audio.blob(sha)
            if hashlib.sha256(data).hexdigest()!=sha:raise ValueError('corrupt recording')
            path=audio_dir/(r['id']+'.wav');path.write_bytes(data);path.chmod(0o600)
            split='validation' if find(r['sessionId']) in validation else 'train'
            output[split].append({'audio':str(path.resolve()),'text':r['corrected'],'id':r['id'],'sessionId':r['sessionId'],
                                 'recognized':r['recognized'],'engine':r['engine']})
        for split,items in output.items():
            path=destination/(split+'.jsonl');path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in items));path.chmod(0o600)
        report={'reviewed':len(rows),'exported':sum(map(len,output.values())),'train':len(output['train']),
                'validation':len(output['validation']),'audioVariant':'clean' if clean else 'raw',
                'readyToTrain':bool(output['train'] and output['validation'])}
        (destination/'report.json').write_text(json.dumps(report,indent=2));return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state',required=True)
    parser.add_argument('command',choices=['list','export']);parser.add_argument('destination',nargs='?');parser.add_argument('--clean',action='store_true')
    args=parser.parse_args();store=SpeechDataset(args.state)
    print(json.dumps(store.list() if args.command=='list' else store.export(args.destination,args.clean),ensure_ascii=False,indent=2))
