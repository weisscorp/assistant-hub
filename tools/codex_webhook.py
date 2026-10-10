#!/usr/bin/env python3
"""Post a terminal Codex app-server turn from stdin to the Hub event webhook.

This adapter publishes data only: it cannot send prompts or approve Codex tools.
Use a webhook-scoped key in a private file, never a key in the URL/arguments.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import quote
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'backend'))
from agent_hub import encoded, text
from hub_codex import turn_report, TERMINAL, LABELS
from hub_node import NodeClient
from hub_providers import request_json


def payload(host, native_id, title, turn):
    text(host,128);text(native_id,256);text(title,160)
    report=turn_report(turn)
    if report['status'] not in TERMINAL:
        raise ValueError('Only terminal turns are completion events')
    event_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'codex:'+host+':'+native_id+':'+report['turn_id']+':'+report['status']))
    return event_id,{'version':1,'type':'codex.turn.'+report['status'],'host':host,
                     'thread_id':native_id,'title':title,**report}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url',required=True)
    p.add_argument('--token-file',type=Path,required=True)
    p.add_argument('--host',required=True)
    p.add_argument('--thread-id',required=True)
    p.add_argument('--title',required=True)
    p.add_argument('--targets',choices=['auto','phone','desktop','both'],default='auto')
    args=p.parse_args()
    client=NodeClient(args.url,args.token_file)
    info=args.token_file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>8192:
        p.error('Use an owned private credential file (0600)')
    raw=sys.stdin.buffer.read(65537)
    if not 0<len(raw)<=65536:
        p.error('Input must be 1..65536 bytes')
    value=json.loads(raw)
    if value.get('method')=='turn/completed':
        value=value['params']['turn']
    event_id,body=payload(args.host,args.thread_id,args.title,value)
    response=request_json(client.url+'/webhooks/events',body,{
        'Authorization':'Bearer '+args.token_file.read_text().strip(),
        'Idempotency-Key':event_id,'X-Event-Source':'codex',
        'X-Event-Title':quote(('Codex: '+args.title+' — '+LABELS[body['status']])[:160]),
        'X-Event-Targets':args.targets},timeout=15)
    print(encoded({'id':response['id'],'status':response['status']}))


if __name__=='__main__':
    main()
