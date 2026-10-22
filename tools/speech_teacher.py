#!/usr/bin/env python3
"""Offline large-ASR teacher: speech timestamps -> clips -> explicit pseudo-label manifests."""
import argparse
import hashlib
import json
import math
import re
from pathlib import Path
import sys
import wave

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from speech_dataset import SpeechDataset


def accepted(segment):
    return (1 <= segment.end-segment.start <= 20 and bool(re.search('[а-яё]',segment.text.lower()))
            and not re.search('[A-Za-z0-9]',segment.text) and math.isfinite(segment.avg_logprob) and segment.avg_logprob >= -1.0
            and segment.no_speech_prob < .5 and segment.compression_ratio < 2.4)


def pieces(segment):
    words=getattr(segment,'words',None)
    if not words or segment.end-segment.start<=18:
        yield segment;return
    from types import SimpleNamespace
    group=[]
    def part(items):
        return SimpleNamespace(start=items[0].start,end=items[-1].end,text=''.join(w.word for w in items),
            avg_logprob=segment.avg_logprob,no_speech_prob=segment.no_speech_prob,compression_ratio=segment.compression_ratio)
    for word in words:
        if group and word.end-group[0].start>18:
            yield part(group);group=[]
        group.append(word)
    if group:yield part(group)


def clip(source,destination,start,end):
    with wave.open(str(source),'rb') as audio:
        rate=audio.getframerate()
        begin=max(0,int((start-.12)*rate));finish=min(audio.getnframes(),int((end+.12)*rate))
        if finish<=begin:raise ValueError('empty teacher segment')
        audio.setpos(begin);data=audio.readframes(finish-begin)
        with wave.open(str(destination),'wb') as out:
            out.setnchannels(audio.getnchannels());out.setsampwidth(audio.getsampwidth());out.setframerate(rate);out.writeframes(data)
    destination.chmod(0o600)


def split_rows(rows):
    # Split by recording session. Duplicate source recordings cannot straddle validation.
    parent={r['sessionId']:r['sessionId'] for r in rows}
    def find(key):
        while parent[key]!=key:key=parent[key]
        return key
    seen={}
    for row in rows:
        sha=row['sourceSha256']
        if sha in seen:parent[find(row['sessionId'])]=find(seen[sha])
        else:seen[sha]=row['sessionId']
    groups=sorted({find(r['sessionId']) for r in rows},key=lambda value:hashlib.sha256(value.encode()).hexdigest())
    validation=set(groups[-max(1,len(groups)//5):]) if len(groups)>1 else set()
    return {'train':[r for r in rows if find(r['sessionId']) not in validation],
            'validation':[r for r in rows if find(r['sessionId']) in validation]}


def run(store,teacher,destination,sources):
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    audio_dir=destination/'audio';audio_dir.mkdir(exist_ok=True,mode=0o700)
    rows=[];audit=[];discarded=0;seen=set()
    for sample in store.rows():
        if sample['deleted'] or sample['origin'] not in sources:continue
        sha=sample['rawSha256']
        if sha in seen:continue
        seen.add(sha)
        source=store.audio.blobs/(sha+'.wav')
        store.audio.blob(sha) # Fail on missing original; never invent an audio/label pair.
        segments,_=teacher.transcribe(str(source),language='ru',beam_size=5,vad_filter=True,condition_on_previous_text=False,word_timestamps=True)
        for parent_number,parent in enumerate(segments):
            for part_number,segment in enumerate(pieces(parent)):
                number=str(parent_number)+"-"+str(part_number)
                audit.append({'sourceId':sample['id'],'sessionId':sample['sessionId'],'origin':sample['origin'],
                              'start':segment.start,'end':segment.end,'text':segment.text,'trainingAccepted':accepted(segment),
                              'avgLogprob':segment.avg_logprob,'noSpeechProbability':segment.no_speech_prob})
                if not accepted(segment):discarded+=1;continue
                target=audio_dir/(sample['id']+'-'+str(number)+'.wav')
                clip(source,target,segment.start,segment.end)
                rows.append({'audio':str(target.resolve()),'text':segment.text.strip(),'id':target.stem,
                             'sessionId':sample['sessionId'],'sourceId':sample['id'],'sourceSha256':sha,'origin':sample['origin'],
                             'start':segment.start,'end':segment.end,'labelSource':'teacher','humanReviewed':False,
                             'avgLogprob':segment.avg_logprob,'noSpeechProbability':segment.no_speech_prob})
    audit_path=destination/'transcripts.jsonl';audit_path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in audit));audit_path.chmod(0o600)
    partitions=split_rows(rows)
    for split,items in partitions.items():
        path=destination/(split+'.jsonl');path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in items));path.chmod(0o600)
    report={'clips':len(rows),'filtered':discarded,'train':len(partitions['train']),'validation':len(partitions['validation']),
            'labels':'pseudo-labels, not human ground truth','readyToTrain':bool(partitions['train'] and partitions['validation'])}
    (destination/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path);parser.add_argument('--model',default='large-v3')
    parser.add_argument('--device',choices=['cpu','cuda'],default='cpu');parser.add_argument('--compute-type',default='int8')
    parser.add_argument('--sources',default='phone,desktop-mic,desktop-system')
    args=parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):parser.error('output directory must be empty')
    store=SpeechDataset(args.state)
    if not any(not r['deleted'] for r in store.rows()):parser.error('No real audio yet. Record dictation or meetings first.')
    from faster_whisper import WhisperModel
    teacher=WhisperModel(args.model,device=args.device,compute_type=args.compute_type)
    report=run(store,teacher,args.output,set(args.sources.split(',')));report['teacherModel']=args.model
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))
