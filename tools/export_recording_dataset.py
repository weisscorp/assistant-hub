#!/usr/bin/env python3
"""Cut bounded training candidates from timestamp manifests; preserve source WAVs."""
import argparse,hashlib,json,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from recording_archive import atomic_text
from timed_transcript import audio_identity
from speech_teacher import split_rows


def export(manifests,destination,labels='corrected'):
    import soundfile as sf
    from scipy.signal import resample_poly
    destination=Path(destination)
    if destination.exists() and any(destination.iterdir()):raise ValueError('Dataset destination must be empty')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700);audio_dir=destination/'audio';audio_dir.mkdir(mode=0o700)
    rows=[];seen=set();identities={};skipped=0
    for manifest in manifests:
        for line in Path(manifest).read_text().splitlines():
            value=json.loads(line)
            if not value.get('candidateForTraining'):skipped+=1;continue
            source=Path(value['audio'])
            if str(source) not in identities:identities[str(source)]=audio_identity(source)
            identity=identities[str(source)]
            if identity['audioSha256']!=value['audioSha256']:raise ValueError('Source WAV changed after timing alignment')
            rate=identity['sampleRate'];start=value['start'];end=value['end']
            if not 0<=start<end<=identity['durationSeconds'] or end-start>15:raise ValueError('Invalid dataset interval')
            key=(value['sessionId'],value['audioSha256'],value['startSample'],value['endSample'])
            if key in seen:continue
            seen.add(key);identifier=hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
            first=max(0,math.floor((start-.12)*rate));last=min(identity['audioFrames'],math.ceil((end+.12)*rate))
            with sf.SoundFile(source) as audio:
                audio.seek(first);samples=audio.read(last-first,dtype='float32',always_2d=True).mean(axis=1)
            divisor=math.gcd(rate,16000)
            converted=resample_poly(samples,16000//divisor,rate//divisor) if rate!=16000 else samples
            target=audio_dir/(identifier+'.wav')
            with target.open('wb') as stream:
                target.chmod(0o600);sf.write(stream,converted,16000,format='WAV',subtype='PCM_16')
            clip_start=first/rate
            row={'id':identifier,'audio':str(target.resolve()),'text':value['asrText'] if labels=='asr' else value['text'],
                 'asrText':value['asrText'],'sessionId':value['sessionId'],'sourceSha256':value['audioSha256'],
                 'sourceAudio':str(source),'start':start,'end':end,'clipStart':clip_start,'clipEnd':last/rate,
                 'labelSource':'asr' if labels=='asr' else value['labelSource'],'humanReviewed':False,
                 'words':[{**w,'word':w['asrWord'] if labels=='asr' else w.get('word',w['asrWord']),
                           'textCorrected':False if labels=='asr' else w.get('textCorrected',False),
                           'labelSource':'asr' if labels=='asr' else w.get('labelSource','asr'),
                           'start':w['start']-clip_start,'end':w['end']-clip_start} for w in value['words']]}
            rows.append(row)
    partitions=split_rows(rows)
    for name,items in partitions.items():
        atomic_text(destination/(name+'.jsonl'),''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in items))
    report={'clips':len(rows),'skippedLowConfidence':skipped,'train':len(partitions['train']),'validation':len(partitions['validation']),
            'sampleRate':16000,'channels':1,'labels':'machine pseudo-labels; not human ground truth',
            'splitBy':'session and identical source audio','readyToTrain':bool(partitions['train'] and partitions['validation'])}
    atomic_text(destination/'report.json',json.dumps(report,ensure_ascii=False,indent=2));return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--manifest',type=Path,action='append',required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--labels',choices=['asr','corrected'],default='corrected')
    args=parser.parse_args();print(json.dumps(export(args.manifest,args.output,args.labels),ensure_ascii=False))
