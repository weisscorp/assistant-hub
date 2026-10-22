"""Audio-relative, machine-labelled transcription timing for dataset segmentation."""
import bisect,copy,hashlib,json,math,re,os
from pathlib import Path
from recording_archive import atomic_text

VERSION=1


def finite(value):
    try:return float(value) if math.isfinite(float(value)) else None
    except (ValueError,TypeError):return None


def capture_segments(segments,recognition_audio,session,track,checkpoint=None,progress=None,recognition=None):
    """Consume the ASR generator once, retaining word alignment and confidence."""
    rows=[];parts=[];offset=0;invalid=0
    if checkpoint:atomic_text(checkpoint,'')
    for segment in segments:
        text=segment.text.strip()
        if not text:continue
        start=finite(getattr(segment,'start',None));end=finite(getattr(segment,'end',None))
        row={'id':len(rows),'asrText':text,'charStart':offset,'charEnd':offset+len(text),
             'start':start,'end':end,'words':[]}
        cursor=0
        for word in getattr(segment,'words',None) or []:
            token=word.word.strip();a=finite(word.start);b=finite(word.end)
            if a is None or b is None or a<0 or b<a:invalid+=1;continue
            index=text.find(token,cursor) if token else -1
            item={'asrWord':token,'start':a,'end':b,'probability':finite(getattr(word,'probability',None)),
                  'probabilitySource':'asr-original-word','charStart':index if index>=0 else None,'charEnd':index+len(token) if index>=0 else None}
            if index>=0:cursor=index+len(token)
            row['words'].append(item)
        for attr in ('avg_logprob','no_speech_prob','compression_ratio'):
            row[attr]=finite(getattr(segment,attr,None))
        row['timingValid']=start is not None and end is not None and 0<=start<end
        if not row['timingValid']:invalid+=1
        rows.append(row);parts.append(text);offset+=len(text)+1
        if checkpoint:
            with Path(checkpoint).open('a') as stream:
                stream.write(json.dumps({**row,'sessionId':session,'track':track,'recognitionAudio':str(recognition_audio),'humanReviewed':False,'recognition':recognition},ensure_ascii=False)+'\n')
                stream.flush();os.fsync(stream.fileno())
        if progress:progress(len(rows),end)
    raw='\n'.join(parts)+'\n' if parts else 'Речь не распознана.\n'
    return raw,{'version':VERSION,'sessionId':session,'track':track,'asrModel':'faster-whisper-small',
                'recognitionAudio':str(recognition_audio),'recognition':recognition,'timeBase':'seconds-from-start-of-each-audio-file',
                'timingSource':'whisper-word-alignment','humanReviewed':False,'rawTextSha256':hashlib.sha256(raw.encode()).hexdigest(),
                'rawText':raw,'invalidTimingCount':invalid,'segments':rows}


def corrected_segments(document,corrected):
    """Text edits keep existing word intervals; never invent timing for added words."""
    result=copy.deepcopy(document);segments=result['segments'];raw=document['rawText']
    raw_words=list(re.finditer(r'\w+',raw));new_words=list(re.finditer(r'\w+',corrected))
    if len(raw_words)!=len(new_words):
        result['correctionAlignment']='needs-realignment';return result
    mapping={m.start():new_words[i].group() for i,m in enumerate(raw_words)}
    starts=[m.start() for m in raw_words];ends=[m.end() for m in raw_words]
    # The editor preserves line breaks, so each original ASR segment stays intact.
    lines=corrected.rstrip('\n').split('\n') if segments else []
    if len(lines)!=len(segments) or any(len(re.findall(r'\w+',line))!=len(re.findall(r'\w+',segment['asrText'])) for line,segment in zip(lines,segments)):
        result['correctionAlignment']='needs-realignment';return result
    for segment,line in zip(segments,lines):
        segment['text']=line;segment['textCorrected']=line!=segment['asrText']
        for word in segment['words']:
            word['word']=word['asrWord'];word['textCorrected']=False;word['labelSource']='asr'
            if word['charStart'] is None:continue
            start=segment['charStart']+word['charStart'];end=segment['charStart']+word['charEnd']
            lo=bisect.bisect_left(starts,start);hi=bisect.bisect_right(ends,end)
            tokens=raw_words[lo:hi] if hi>=lo else []
            if tokens:
                updated=word['asrWord']
                for token in reversed(tokens):
                    a=token.start()-start;b=token.end()-start
                    updated=updated[:a]+mapping[token.start()]+updated[b:]
                if updated!=word['asrWord']:word['word']=updated;word['textCorrected']=True;word['labelSource']='context-review'
    result['correctionAlignment']='word-count-and-segment-boundaries-preserved'
    return result


def stamp(seconds,srt=False):
    millis=round(seconds*1000);hours,millis=divmod(millis,3600000);minutes,millis=divmod(millis,60000);secs,millis=divmod(millis,1000)
    return f'{hours:02}:{minutes:02}:{secs:02}{"," if srt else "."}{millis:03}'


def audio_identity(audio):
    import soundfile as sf
    path=Path(audio);info=sf.info(path);stat=path.stat();cache=path.with_suffix('.audio-sha256.json')
    identity={'size':stat.st_size,'mtimeNs':stat.st_mtime_ns,'path':str(path)}
    value=json.loads(cache.read_text()) if cache.is_file() else {}
    if any(value.get(k)!=v for k,v in identity.items()):
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        value={**identity,'sha256':digest.hexdigest()};atomic_text(cache,json.dumps(value))
    return {'audioSha256':value['sha256'],'sampleRate':info.samplerate,'channels':info.channels,'audioFrames':info.frames,'durationSeconds':info.duration}


def training_rows(document,audio,identity,max_seconds=15):
    """Bounded utterances, paired with the noisy/original or derived audio variant."""
    for segment in document['segments']:
        if not segment['timingValid']:continue
        words=segment['words'];groups=[];current=[]
        for word in words:
            if current and word['end']-current[0]['start']>max_seconds:groups.append(current);current=[]
            current.append(word)
        if current:groups.append(current)
        if not words:groups=[[]]
        for part,group in enumerate(groups):
            start=group[0]['start'] if group else segment['start'];end=group[-1]['end'] if group else segment['end']
            if not 0<=start<end<=identity['durationSeconds']+1e-6:continue
            raw=' '.join(w['asrWord'] for w in group) if group else segment['asrText']
            text=' '.join(w.get('word',w['asrWord']) for w in group) if group else segment.get('text',segment['asrText'])
            changed=raw!=text
            quality=(1<=end-start<=max_seconds and bool(group) and
                     segment['avg_logprob'] is not None and segment['avg_logprob']>=-1 and
                     segment['no_speech_prob'] is not None and 0<=segment['no_speech_prob']<.5 and
                     segment['compression_ratio'] is not None and 0<segment['compression_ratio']<2.4 and
                     all(w['end']>w['start'] and w['probability'] is not None and .1<=w['probability']<=1 and w['charStart'] is not None for w in group) and
                     all(group[i]['start']>=group[i-1]['end']-.001 for i in range(1,len(group))) and
                     all(segment['start']-.05<=w['start']<w['end']<=segment['end']+.05 for w in group) and
                     document.get('correctionAlignment')!='needs-realignment')
            yield {'id':f"{document['sessionId']}:{document['track']}:{segment['id']}:{part}",
                   'sessionId':document['sessionId'],'splitGroup':document['sessionId'],'audio':str(audio),
                   'recognitionAudio':document['recognitionAudio'],**identity,'start':start,'end':end,
                   'startSample':round(start*identity['sampleRate']),'endSample':round(end*identity['sampleRate']),
                   'asrText':raw,'text':text,'words':group,'labelSource':'asr+context-review' if changed else 'asr',
                   'asrModel':document['asrModel'],'recognition':document.get('recognition'),'humanReviewed':False,'candidateForTraining':bool(quality),
                   'timingSource':document['timingSource'],'segmentId':segment['id']}


def write_timed_outputs(audio,document,corrected):
    audio=Path(audio)
    if not any(s['timingValid'] for s in document['segments']):return {'phase':'unavailable','segments':0,'words':0}
    if hashlib.sha256(document['rawText'].encode()).hexdigest()!=document['rawTextSha256']:raise ValueError('ASR timing text hash mismatch')
    aligned=corrected_segments(document,corrected)
    if aligned.get('correctionAlignment')=='needs-realignment':return {'phase':'needs-realignment','segments':0,'words':0}
    identity=audio_identity(audio);aligned.update(identity);aligned['audio']=str(audio)
    # Alias IDs must stay distinct even though recognition was shared.
    variant=audio.stem.rsplit('_',1)[-1];aligned['track']=variant
    valid=[s for s in aligned['segments'] if s['timingValid'] and s['end']<=identity['durationSeconds']+1e-6]
    subtitles=[];timed=[]
    for index,segment in enumerate(valid,1):
        text=segment.get('text',segment['asrText']);a=segment['start'];b=segment['end']
        subtitles.append(f'{index}\n{stamp(a,True)} --> {stamp(b,True)}\n{text}\n')
        timed.append(f'[{stamp(a)} – {stamp(b)}] {text}')
    atomic_text(audio.with_suffix('.plain.txt'),corrected)
    atomic_text(audio.with_suffix('.txt'),'\n'.join(timed)+'\n')
    atomic_text(audio.with_suffix('.srt'),'\n'.join(subtitles))
    atomic_text(audio.with_suffix('.timestamps.json'),json.dumps(aligned,ensure_ascii=False,indent=2))
    rows=list(training_rows(aligned,audio,identity))
    atomic_text(audio.with_suffix('.segments.jsonl'),''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    return {'phase':'ready','segments':len(valid),'words':sum(len(s['words']) for s in valid),'datasetRows':len(rows),
            'trainingCandidates':sum(r['candidateForTraining'] for r in rows),'humanReviewed':False}


def refresh_timed_outputs(texts):
    statuses={}
    for target,corrected in texts:
        target=Path(target);source=target.with_suffix('.asr.json')
        if source.is_file():
            document=json.loads(source.read_text())
            if document['rawTextSha256']!=hashlib.sha256(target.with_suffix('.asr.txt').read_bytes()).hexdigest():
                statuses[str(target)]={'phase':'needs-realignment'};continue
            statuses[str(target)]=write_timed_outputs(target.with_suffix('.wav'),document,corrected)
    return statuses
