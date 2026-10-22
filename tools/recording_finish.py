#!/usr/bin/env python3
"""Finalize original PCM files and produce full, same-name text after stopping."""
import argparse,json,sys,time,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from recording_archive import atomic_text,export_audio,session_lock
from acoustic_echo import export_clean,VERSION as ECHO_VERSION
from voice_cleanup import export_voice,VERSION as VOICE_VERSION
from transcript_review import configured_client,review_outputs
from timed_transcript import capture_segments,refresh_timed_outputs
from speech_recognition import transcribe_audio,recognition_metadata

def _finish(directory,model=None,refresh_echo=False,reviewer=None):
    directory=Path(directory);path=directory/'archive.json';meta=json.loads(path.read_text())
    production=model is None
    was_ready=meta['phase']=='ready'
    if was_ready and not refresh_echo:return meta
    try:
        meta['error']='';meta['phase']='saving';atomic_text(path,json.dumps(meta))
        # Explicit refresh reuses completed native files byte for byte.
        files=[]
        if was_ready and refresh_echo:
            for track,suffix in (('mic','_микрофон.wav'),('system','_звук-Mac.wav')):
                audio=Path(meta['archive'])/(meta['base']+suffix)
                if audio.is_file():files.append((track,audio))
        if not files:files=export_audio(directory)
        if not files:meta['phase']='empty';atomic_text(path,json.dumps(meta));return meta
        originals=dict(files);clean=None
        if 'mic' in originals and 'system' in originals:
            meta['processing']='echo-cancellation';atomic_text(path,json.dumps(meta))
            clean=export_clean(originals['mic'],originals['system'])
        voice=export_voice(clean or originals['mic']) if 'mic' in originals else None
        meta['voiceCleanup']='rnnoise' if voice else 'unavailable'
        meta['phase']='transcribing';atomic_text(path,json.dumps(meta))
        if model is None:
            from faster_whisper import WhisperModel
            model=WhisperModel('small',device='cpu',compute_type='int8',cpu_threads=3)
        texts=[];timings={}
        for track,audio in files:
            recognition=(voice or clean or audio) if track=='mic' else audio
            segments,info=transcribe_audio(model,str(recognition))
            raw,alignment=capture_segments(segments,recognition,meta['session'],track,checkpoint=audio.with_suffix('.asr-segments.jsonl'),recognition=recognition_metadata(info))
            timings[track]=alignment
            atomic_text(audio.with_suffix('.asr.json'),json.dumps(alignment,ensure_ascii=False))
            atomic_text(audio.with_suffix('.asr.txt'),raw)
            texts.append((audio.with_suffix('.txt'),raw))
        if clean:
            texts.append((clean.with_suffix('.txt'),next(text for path,text in texts if path==originals['mic'].with_suffix('.txt'))))
            atomic_text(clean.with_suffix('.asr.json'),json.dumps(timings['mic'],ensure_ascii=False))
            files.append(('mic-clean',clean))
            meta['echoVersion']=ECHO_VERSION
            meta['microphoneTranscriptSource']=str(clean)
        if voice:
            texts.append((voice.with_suffix('.txt'),next(text for path,text in texts if path==originals['mic'].with_suffix('.txt'))))
            atomic_text(voice.with_suffix('.asr.json'),json.dumps(timings['mic'],ensure_ascii=False))
            files.append(('voice',voice));meta['voiceCleanupVersion']=VOICE_VERSION
            meta['microphoneTranscriptSource']=str(voice)
        if production:model=None # Release the ASR model before the text-only stage.
        if reviewer is None and production:
            try:reviewer=configured_client()
            except Exception as error:meta['textReviewConfigError']=type(error).__name__
        meta['processing']='text-review'
        with session_lock(meta['archive'],'text-review-worker'):
            texts=review_outputs(meta,texts,reviewer,lambda:atomic_text(path,json.dumps(meta)))
        if meta.get('textReviewConfigError'):meta['textReview']['phase']='unavailable'
        with session_lock(meta['archive'],meta['session']):
            for text_path,text in texts:atomic_text(text_path,text)
            meta['timestamps']=refresh_timed_outputs(texts)
            meta['phase']='ready';meta['completedAt']=time.time();meta['files']=[str(audio) for _,audio in files];atomic_text(path,json.dumps(meta))
    except Exception as error:
        meta['phase']='error';meta['error']=str(error)[:600];atomic_text(path,json.dumps(meta));raise
    return meta

def finish(directory,model=None,refresh_echo=False,reviewer=None):
    if model is not None:return _finish(directory,model,refresh_echo,reviewer)
    metadata=json.loads((Path(directory)/'archive.json').read_text())
    # One full-model worker at a time; incremental base transcription stays independent.
    with session_lock(metadata['archive'],'full-transcription-worker'):
        return _finish(directory,refresh_echo=refresh_echo,reviewer=reviewer)

def review_only(directory,reviewer=None,progress=None):
    directory=Path(directory);path=directory/'archive.json';meta=json.loads(path.read_text())
    if meta.get('phase')!='ready':raise ValueError('Дождитесь завершения распознавания перед повторной правкой текста')
    reviewer=reviewer or configured_client()
    if reviewer is None:raise ValueError('Локальная модель для правки транскрипта не настроена')
    texts=[]
    for audio in meta.get('files',[]):
        target=Path(audio).with_suffix('.txt');raw=target.with_suffix('.asr.txt')
        source=raw if raw.is_file() else target
        if source.is_file():texts.append((target,source.read_text()))
    with session_lock(meta['archive'],'text-review-worker'):
        texts=review_outputs(meta,texts,reviewer,lambda:atomic_text(path,json.dumps(meta)),progress)
        with session_lock(meta['archive'],meta['session']):
            for target,text in texts:atomic_text(target,text)
            meta['timestamps']=refresh_timed_outputs(texts)
            atomic_text(path,json.dumps(meta))
    return meta

def retime(directory,model=None,reviewer=None,progress=None):
    """Re-read existing audio for actual word timing; never rerun audio cleanup."""
    directory=Path(directory);path=directory/'archive.json';meta=json.loads(path.read_text())
    if meta.get('phase') not in ('ready','error'):raise ValueError('Дождитесь завершения записи/обработки')
    production=model is None;files=[Path(p) for p in meta.get('files',[]) if Path(p).is_file()]
    if not files:raise ValueError('Для временных меток нужны сохранённые WAV')
    backup=directory/('before-word-timestamps-'+str(time.time_ns()));backup.mkdir(mode=0o700)
    shutil.copy2(path,backup/'archive.json')
    for audio in files:
        for suffix in ('.txt','.asr.txt','.plain.txt','.review.json','.asr.json','.asr-segments.jsonl','.timestamps.json','.srt','.segments.jsonl'):
            source=audio.with_suffix(suffix)
            if source.is_file():shutil.copy2(source,backup/source.name)
    try:
        meta['phase']='transcribing';meta['processing']='word-timestamps';atomic_text(path,json.dumps(meta))
        data={};texts=[]
        with session_lock(meta['archive'],'full-transcription-worker'):
            if model is None:
                from faster_whisper import WhisperModel
                model=WhisperModel('small',device='cpu',compute_type='int8',cpu_threads=3)
            for audio in files:
                variant=audio.stem.rsplit('_',1)[-1]
                track='system' if variant=='звук-Mac' else 'mic'
                recognition=audio if track=='system' else Path(meta.get('microphoneTranscriptSource',str(audio)))
                if not recognition.is_file():recognition=audio
                if str(recognition) not in data:
                    segments,info=transcribe_audio(model,str(recognition))
                    raw,alignment=capture_segments(segments,recognition,meta['session'],track,
                        checkpoint=audio.with_suffix('.asr-segments.jsonl'),progress=progress,recognition=recognition_metadata(info))
                    data[str(recognition)]=(raw,alignment)
                raw,alignment=data[str(recognition)]
                atomic_text(audio.with_suffix('.asr.json'),json.dumps(alignment,ensure_ascii=False))
                atomic_text(audio.with_suffix('.asr.txt'),raw);texts.append((audio.with_suffix('.txt'),raw))
        if production:model=None
        if reviewer is None and production:reviewer=configured_client()
        meta['processing']='text-review'
        with session_lock(meta['archive'],'text-review-worker'):
            texts=review_outputs(meta,texts,reviewer,lambda:atomic_text(path,json.dumps(meta)))
            with session_lock(meta['archive'],meta['session']):
                for target,text in texts:atomic_text(target,text)
                meta['timestamps']=refresh_timed_outputs(texts)
                meta['phase']='ready';atomic_text(path,json.dumps(meta))
        return meta
    except Exception as error:
        meta['phase']='error';meta['error']=str(error)[:400];atomic_text(path,json.dumps(meta));raise

def glossary_only(directory,glossary_path=None):
    """Apply terminology to existing audits with backups; leave raw ASR untouched."""
    from transcript_review import reapply_glossary
    from speech_glossary import load
    directory=Path(directory);path=directory/'archive.json'
    with session_lock(directory,'glossary-repair'):
        meta=json.loads(path.read_text())
        if meta.get('phase')!='ready':raise ValueError('Recording is still processing')
        with session_lock(meta['archive'],'text-review-worker'),session_lock(meta['archive'],meta['session']):
            data=load(glossary_path);outputs=[]
            for audio in meta.get('files',[]):
                target=Path(audio).with_suffix('.txt');raw=target.with_suffix('.asr.txt');audit=target.with_suffix('.review.json')
                if not raw.exists() or not audit.exists():continue
                corrected,report=reapply_glossary(raw.read_text(),json.loads(audit.read_text()),data)
                outputs.append((target,corrected,report))
            if not outputs:raise ValueError('No completed ASR review audits')
            backup=directory/('before-glossary-'+str(time.time_ns()));backup.mkdir(mode=0o700)
            shutil.copy2(path,backup/'archive.json')
            for target,_,_ in outputs:
                for suffix in ('.txt','.plain.txt','.review.json','.srt','.timestamps.json','.segments.jsonl'):
                    source=target.with_suffix(suffix)
                    if source.exists():shutil.copy2(source,backup/source.name)
            for target,text,report in outputs:
                atomic_text(target,text);atomic_text(target.with_suffix('.review.json'),json.dumps(report,ensure_ascii=False,indent=2))
            meta['timestamps']=refresh_timed_outputs([(p,t) for p,t,_ in outputs])
            unique={r['rawSha256']:r for _,_,r in outputs}
            meta.setdefault('textReview',{})['acceptedEdits']=sum(r['acceptedEdits'] for r in unique.values())
            meta['textReview']['glossaryApplied']=True
            atomic_text(path,json.dumps(meta,ensure_ascii=False))
            return meta

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',required=True)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--refresh-echo',action='store_true',help='Process an already finalized recording; reuse original WAV files unchanged')
    mode.add_argument('--retime',action='store_true',help='Re-read existing WAV for word timestamps; keep originals and back up previous text outputs')
    mode.add_argument('--review-only',action='store_true',help='Correct existing TXT only; never read or transcribe WAV again')
    mode.add_argument('--glossary-only',action='store_true',help='Reapply glossary to cached reviews without audio or model calls')
    args=parser.parse_args()
    if args.glossary_only:
        result=glossary_only(args.directory)
        print(json.dumps(result['textReview'],ensure_ascii=False))
    elif args.retime:
        result=retime(args.directory,progress=lambda count,end:print(f'alignment: {count} segments, {end} seconds',flush=True) if count%20==0 else None)
        print(json.dumps(result['timestamps'],ensure_ascii=False))
    elif args.review_only:
        result=review_only(args.directory,progress=lambda done,total,edits,state:print(f'text review {done}/{total}: {edits} accepted edits, {state}',flush=True))
        print(json.dumps(result['textReview'],ensure_ascii=False))
    else:finish(args.directory,refresh_echo=args.refresh_echo)
