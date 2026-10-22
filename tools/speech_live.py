#!/usr/bin/env python3
"""Optional near-live transcription of completed capture pieces; never changes raw audio."""
import argparse,json,os,sys,time,subprocess,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from speech_dataset import SpeechDataset
from recording_archive import atomic_text,write_partial
from acoustic_echo import cancel_echo,decode,capture_reference
from voice_cleanup import enhance_samples,model_path
from speech_recognition import transcribe_audio,recognition_metadata

def transcribe_pending(store,model,session,output,directory=None):
    seen={json.loads(line)['id'] for line in output.read_text().splitlines()} if output.exists() else set()
    rows=[r for r in store.rows() if r['sessionId']==session and not r['deleted'] and r['id'] not in seen]
    for row in sorted(rows,key=lambda r:(r['createdAt'],r['id'])):
        audio=str(store.audio.blobs/(row['rawSha256']+'.wav'))
        echo_applied=False
        if directory and row['origin']=='desktop-mic':
            for chunk in Path(directory).glob('mic-*.raw.caf'):
                name=chunk.name.replace('.raw.caf','.ready.wav')
                if str(uuid.uuid5(uuid.UUID(session),name))!=row['id']:continue
                reference=capture_reference(directory,chunk.name.split('-')[1].split('.')[0])
                if reference is not None:
                    audio=cancel_echo(decode(audio),reference);echo_applied=True
                break
        voice_applied=row['origin']=='desktop-mic' and model_path() is not None
        if voice_applied:audio=enhance_samples(decode(audio) if isinstance(audio,str) else audio)
        segments,info=transcribe_audio(model,audio,preview=True,vad_filter=False,word_timestamps=True)
        segments=list(segments)
        end_ms=row['createdAt'];start_ms=end_ms-row['durationMs']
        words=[]
        for segment in segments:
            segment_words=getattr(segment,'words',None)
            if segment_words:
                words.extend({'text':word.word.strip(),'startMs':start_ms+int(word.start*1000),'endMs':start_ms+int(word.end*1000)} for word in segment_words)
            else:
                words.append({'text':segment.text.strip(),'startMs':start_ms+int(getattr(segment,'start',0)*1000),'endMs':start_ms+int(getattr(segment,'end',row['durationMs']/1000)*1000)})
        # Keep even uncertain hypotheses as a preview, never pretend these are human labels.
        result={'id':row['id'],'sessionId':session,'origin':row['origin'],'text':' '.join(s.text.strip() for s in segments),'labelSource':'whisper-base','humanReviewed':False,'startMs':start_ms,'endMs':end_ms,'words':words,'sourceSha256':row['rawSha256'],'echoReferenceUsed':echo_applied,'voiceCleanupUsed':voice_applied,'recognition':recognition_metadata(info)}
        with output.open('a') as stream:stream.write(json.dumps(result,ensure_ascii=False)+'\n');stream.flush();os.fsync(stream.fileno())
        output.chmod(0o600)
    if directory and output.exists():write_partial(directory,[json.loads(line) for line in output.read_text().splitlines()])
    return len(rows)

def transcript_preview(output,window_ms=15000):
    """Time-based tail of the latest recognized audio, including both concurrent tracks."""
    if not output.is_file():return ''
    from collections import deque
    with output.open('rb') as stream:
        offset=max(0,output.stat().st_size-262144);stream.seek(offset);text=stream.read().decode(errors='ignore')
    lines=text.splitlines()
    if offset:lines=lines[1:]
    lines=lines[-20:]
    rows=[]
    for line in lines:
        try:rows.append(json.loads(line))
        except ValueError:continue
    if not rows:return ''
    end=max(row.get('endMs',0) for row in rows)
    if not end:return rows[-1].get('text','')[-500:] # Older transcript files.
    cutoff=end-window_ms;parts=[]
    for origin in ('desktop-mic','desktop-system','phone'):
        words=[word for row in rows if row.get('origin')==origin for word in row.get('words',[]) if word['endMs']>cutoff and word['startMs']<end]
        text=' '.join(word['text'] for word in sorted(words,key=lambda word:word['startMs']))
        if text:parts.append(text)
    return ' · '.join(parts)[-600:]

def partial_preview(directory,model,positions):
    """Decode a snapshot of the growing ASR WAV; raw originals remain untouched."""
    import numpy as np
    directory=Path(directory);parts=[];updated=False;captured_at=time.time()
    previous=directory/'partial-preview.json'
    old=json.loads(previous.read_text()) if previous.exists() else {}
    by_track=old.get('tracks',{});paths=old.get('paths',{})
    for path in sorted(directory.glob('*.part.wav')):
        track=path.name.split('-')[0];size=path.stat().st_size
        if size-positions.get(str(path),44)<16000*2*4:continue
        try:
            decoded=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-ignore_length','1','-i',str(path),'-map','0:a:0','-f','f32le','-ac','1','-ar','16000','pipe:1'],capture_output=True,timeout=4,check=True).stdout
        except (subprocess.SubprocessError,OSError):continue
        samples=np.frombuffer(decoded[:len(decoded)//4*4],dtype=np.float32).copy()
        if track=='mic':
            reference=capture_reference(directory,path.name.split('-')[1].split('.')[0])
            if reference is not None:samples=cancel_echo(samples,reference)
        if track=='mic' and model_path() is not None:samples=enhance_samples(samples)
        samples=samples[-15*16000:]
        if len(samples)<16000:continue
        segments,_=transcribe_audio(model,samples,preview=True)
        by_track[track]=' '.join(segment.text.strip() for segment in segments);paths[track]=path.name
        positions[str(path)]=size;updated=True
    if updated:
        text=' · '.join(('Мик: ' if track=='mic' else 'Mac: ')+value for track,value in by_track.items() if value)
        atomic_text(previous,json.dumps({'text':text,'tracks':by_track,'paths':paths,'capturedAt':captured_at},ensure_ascii=False))
        complete=directory/'live-transcripts.jsonl'
        write_partial(directory,[json.loads(line) for line in complete.read_text().splitlines()] if complete.exists() else [])
    return updated

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ('state','session','output','capture-state','directory'):parser.add_argument('--'+arg,required=True)
    args=parser.parse_args();directory=Path(args.directory);job=directory/'transcription-state.json'
    try:
        atomic_text(job,json.dumps({'phase':'loading','model':'base'}))
        from faster_whisper import WhisperModel
        model=WhisperModel('base',device='cpu',compute_type='int8',cpu_threads=3)
        store=SpeechDataset(args.state);output=Path(args.output);positions={}
        atomic_text(job,json.dumps({'phase':'live','model':'base'}))
        while True:
            capture=json.loads(Path(args.capture_state).read_text())
            active=capture.get('session')==args.session and capture.get('phase') not in ('stopped','failed','idle')
            if active:partial_preview(directory,model,positions)
            transcribe_pending(store,model,args.session,output,directory)
            if not active:break
            time.sleep(.5)
        atomic_text(job,json.dumps({'phase':'finished','model':'base'}))
    except Exception as error:
        atomic_text(job,json.dumps({'phase':'error','error':str(error)[:500]}));raise

if __name__=='__main__':main()
