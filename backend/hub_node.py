#!/usr/bin/env python3
"""Outbound node worker. Owner-installed Python handlers advertise their actions."""
import argparse
import importlib.util
import json
import platform
from pathlib import Path
import sys
import time
from urllib.parse import urlencode, urlsplit
from hub_fabric import object_schema, identifier, validate_arguments
from hub_providers import request_json


CAPABILITIES = [
    {'name':'system.info','description':'Read this node operating system and architecture. Информация о системе компьютера.', 'inputSchema':object_schema()},
    {'name':'system.time','description':'Read the current UTC time on this node. Текущее время компьютера.', 'inputSchema':object_schema()},
]


def handle(name, arguments, context, client):
    if name == 'system.info':
        return {'system':platform.system(),'release':platform.release(),'architecture':platform.machine()}
    if name == 'system.time':
        from datetime import datetime, timezone
        return {'utc':datetime.now(timezone.utc).isoformat()}
    raise ValueError('No installed handler for this capability')


class NodeClient:
    def __init__(self, url, token_file):
        parsed = urlsplit(url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use an explicit HTTP(S) Hub endpoint without URL credentials')
        self.url, self.token_file = url.rstrip('/'), Path(token_file)

    def request(self, path, body=None):
        # Read on each call so the owner can rotate a private credential file.
        token = self.token_file.read_text().strip()
        if len(token) < 32:
            raise ValueError('Invalid Hub credential file')
        return request_json(self.url+path,body,{'Authorization':'Bearer '+token},timeout=15)

    def context_packet(self, context_id, query):
        return self.request('/api/context?'+urlencode({'id':context_id,'query':query,'budget':16000}))

    def execute_once(self, node, capabilities, handler):
        call = self.request('/api/nodes/'+node+'/claim',{})
        if call is None:
            return False
        try:
            definition = next(c for c in capabilities if c['name']==call['capability'])
            validate_arguments(call['arguments'],definition['inputSchema'])
            result = handler(call['capability'],call['arguments'],
                             {'context_id':call['context_id'],'call_id':call['id'],'task_id':call['task_id']},self)
            # Prohibit non-JSON/NaN results before they cross the protocol.
            wire = json.dumps(result,allow_nan=False)
            if len(wire.encode()) > 12000:
                raise ValueError('Handler output exceeds budget')
            error = ''
        except Exception as exc:
            result, error = None, 'Node handler failed ('+type(exc).__name__+')'
        acknowledgement = {'id':call['id'],'lease':call['lease'],'result':result,'error':error}
        deadline = time.monotonic()+50
        while True:
            try:
                self.request('/api/nodes/'+node+'/result',acknowledgement)
                return True
            except OSError as exc:
                # Retry the acknowledgement, never re-execute the action.
                if getattr(exc,'code',None) in {400,401,403,409} or time.monotonic()>=deadline:
                    print('Node result acknowledgement failed for '+call['id'],file=sys.stderr)
                    return True
                time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--token-file',required=True)
    parser.add_argument('--id',required=True)
    parser.add_argument('--label',required=True)
    parser.add_argument('--kind',default='computer')
    parser.add_argument('--handlers',type=Path,help='Trusted owner-installed Python module exporting CAPABILITIES and handle')
    args = parser.parse_args();identifier(args.id)
    capabilities, handler = CAPABILITIES, handle
    if args.handlers:
        spec=importlib.util.spec_from_file_location('owner_node_handlers',args.handlers.resolve(strict=True))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        capabilities,handler=module.CAPABILITIES,module.handle
    client=NodeClient(args.url,args.token_file)
    try:
        while True:
            try:
                client.request('/api/nodes',{'id':args.id,'label':args.label,'kind':args.kind,'capabilities':capabilities,'ttl':90})
                while True:
                    client.execute_once(args.id,capabilities,handler)
                    time.sleep(1)
            except (OSError,ValueError):
                print('Node waiting for Hub connection',file=sys.stderr)
                time.sleep(3)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
