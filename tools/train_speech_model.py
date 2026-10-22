#!/usr/bin/env python3
"""T-one CTC fine-tuning using the upstream training wrapper and corpus manifests.
No automatic upload or replacement of Android's ONNX runtime model.
"""
import argparse
import json
from pathlib import Path
import re

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--dataset',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
parser.add_argument('--base-model',default='t-tech/T-one');parser.add_argument('--epochs',type=int,default=3)
parser.add_argument('--batch-size',type=int,default=2);parser.add_argument('--allow-pseudo-labels',action='store_true')
parser.add_argument('--device',choices=['cpu','cuda'],default='cpu')
args=parser.parse_args()
rows={name:[json.loads(line) for line in (args.dataset/(name+'.jsonl')).read_text().splitlines() if line.strip()] for name in ('train','validation')}
if not rows['train'] or not rows['validation']:parser.error('Need separate nonempty train and validation sessions')
if {r['sessionId'] for r in rows['train']} & {r['sessionId'] for r in rows['validation']}:parser.error('Session leakage between train and validation')
if any(r.get('labelSource')=='teacher' for items in rows.values() for r in items) and not args.allow_pseudo_labels:
    parser.error('Teacher labels are preliminary. Review them, or explicitly use --allow-pseudo-labels')
if args.output.exists() and any(args.output.iterdir()):parser.error('output directory must be empty')
if args.epochs<1 or args.batch_size<1:parser.error('epochs and batch size must be positive')

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
import torch
from datasets import Dataset,DatasetDict
from transformers import Wav2Vec2CTCTokenizer,Wav2Vec2FeatureExtractor,Wav2Vec2Processor,TrainingArguments,Trainer
from tone.training.data_collator import DataCollatorCTCWithPadding
from tone.training.model_wrapper import ToneForCTC
from jiwer import wer

processor=Wav2Vec2Processor(feature_extractor=Wav2Vec2FeatureExtractor(feature_size=1,sampling_rate=8000,padding_value=0,return_attention_mask=False,do_normalize=False),
    tokenizer=Wav2Vec2CTCTokenizer.from_pretrained(args.base_model,pad_token='[PAD]',word_delimiter_token='|'))

def prepare(row):
    audio,rate=sf.read(row['audio'],dtype='float32')
    if audio.ndim!=1 or rate!=16000:raise ValueError('Expected mono 16kHz source WAV')
    # The phone corpus preserves 16kHz originals; T-one's training input is 8kHz.
    audio=resample_poly(audio,1,2)
    values=processor(audio,sampling_rate=8000).input_values[0]
    if re.search('[A-Za-z0-9]',row['text']):raise ValueError('T-one labels require Russian words; review numbers and Latin names instead of silently dropping them')
    transcript=' '.join(re.findall('[а-яё]+',row['text'].lower()))
    if not transcript:raise ValueError('No Russian text in a label')
    return {'input_values':np.pad(values,(2400,2400)),'input_lengths':len(values)+4800,'labels':processor(text=transcript).input_ids}

data=DatasetDict({split:Dataset.from_list(items).map(prepare,remove_columns=list(items[0])) for split,items in rows.items()})
data['train']=data['train'].filter(lambda r:r['input_lengths']<=20*8000)
if not len(data['train']):parser.error('No train examples shorter than 20 seconds')
model=ToneForCTC.from_pretrained(args.base_model).to(args.device)

def metrics(prediction):
    ids=np.argmax(prediction.predictions,axis=-1)
    ids[(prediction.predictions==-100).all(axis=-1)]=processor.tokenizer.pad_token_id
    labels=np.where(prediction.label_ids==-100,processor.tokenizer.pad_token_id,prediction.label_ids)
    return {'wer':wer(processor.batch_decode(labels,group_tokens=False),processor.batch_decode(ids))}

training=TrainingArguments(output_dir=str(args.output),per_device_train_batch_size=args.batch_size,per_device_eval_batch_size=args.batch_size,
    gradient_accumulation_steps=8,num_train_epochs=args.epochs,learning_rate=5e-5,weight_decay=1e-6,warmup_ratio=.05,
    eval_strategy='epoch',save_strategy='epoch',logging_strategy='epoch',save_total_limit=2,report_to=[],use_cpu=args.device=='cpu')
trainer=Trainer(model=model,args=training,data_collator=DataCollatorCTCWithPadding(processor=processor,padding=True),
    train_dataset=data['train'],eval_dataset=data['validation'],compute_metrics=metrics,processing_class=processor.feature_extractor)
baseline=trainer.evaluate();trainer.train();after=trainer.evaluate()
trainer.save_model(str(args.output/'final'));processor.save_pretrained(str(args.output/'final'))
(args.output/'comparison.json').write_text(json.dumps({'base':baseline,'finetuned':after,
    'validationLabels':'teacher agreement' if args.allow_pseudo_labels else 'human verified',
    'androidNextStep':'Export and validate a sherpa-compatible streaming ONNX model before replacing runtime assets.'},ensure_ascii=False,indent=2))
print(json.dumps({'baseline':baseline,'finetuned':after},ensure_ascii=False,indent=2))
