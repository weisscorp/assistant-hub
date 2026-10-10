"""Read Codex sessions via app-server JSON-RPC. No turn/start, resume or approvals.

List/import chat references and export a bounded page of context into the hub.
Use --socket to read from an existing app-server instead of spawning one. Native
Codex manages its own authentication; the bridge never reads or exports tokens.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

from agent_hub import HubStore, encoded, number, text


SOURCES = ['cli','vscode','exec','appServer','subAgent','subAgentReview',
           'subAgentCompact','subAgentThreadSpawn','subAgentOther','unknown']
READ_METHODS = {'initialize','thread/list','thread/read','thread/turns/list'}
MAX_FRAME = 8 * 1024 * 1024


class BridgeError(RuntimeError):
    pass


class CodexReader:
    def __init__(self, binary='codex', socket_path=None, timeout=15, *, command=None, environment=None):
        if not 0 < timeout <= 60:
            raise ValueError('invalid timeout')
        if command is None:
            executable = shutil.which(binary)
            if not executable:
                raise BridgeError('Codex executable not found')
            command = [executable, 'app-server']
            if socket_path:
                if not Path(socket_path).is_absolute():
                    raise ValueError('socket path must be absolute')
                command += ['proxy', '--sock', socket_path]
            else:
                command += ['--listen', 'stdio://']
        self.timeout = timeout
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=environment, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.sequence = 0
        try:
            self.server = self.call('initialize', {'clientInfo': {'name':'assistant_context_bridge', 'version':'0.1.0'},
                                                  'capabilities': {'experimentalApi':True}})
            self.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.close()
            raise

    def send(self, message):
        wire = encoded(message).encode('utf-8') + b'\n'
        try:
            view = memoryview(wire)
            while view:
                written = self.process.stdin.write(view)
                if not written:
                    raise BridgeError('Codex input closed')
                view = view[written:]
        except (OSError, ValueError) as error:
            raise BridgeError('Codex input closed') from error

    def receive(self, deadline):
        while True:
            boundary = self.buffer.find(b'\n')
            if boundary >= 0:
                line = bytes(self.buffer[:boundary]); del self.buffer[:boundary+1]
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError) as error:
                    raise BridgeError('invalid Codex JSON frame') from error
                if not isinstance(value, dict):
                    raise BridgeError('invalid Codex frame')
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise BridgeError('Codex read timed out; request was not retried')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise BridgeError('Codex output closed')
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_FRAME:
                raise BridgeError('Codex response too large; use a smaller page')

    def call(self, method, params):
        if method not in READ_METHODS:
            raise ValueError('bridge only supports read methods')
        self.sequence += 1
        request_id = self.sequence
        self.send({'method':method, 'id':request_id, 'params':params})
        deadline = time.monotonic() + self.timeout
        for _ in range(1000):
            value = self.receive(deadline)
            if 'method' in value:
                if 'id' in value:
                    # A read client cannot grant execution or permission requests.
                    self.send({'id':value['id'], 'error':{'code':-32601, 'message':'read-only context bridge'}})
                continue
            if type(value.get('id')) is not int or value['id'] != request_id:
                raise BridgeError('unexpected Codex response ID')
            if 'error' in value:
                code = value['error'].get('code') if isinstance(value['error'], dict) else None
                raise BridgeError(f'{method} failed (RPC code {code})')
            if not isinstance(value.get('result'), dict):
                raise BridgeError('invalid Codex result')
            return value['result']
        raise BridgeError('too many Codex notifications')

    def list_chats(self, limit=50, cursor=None, archived=False):
        number(limit,1,100)
        params = {'limit':limit, 'sourceKinds':SOURCES, 'modelProviders':[],
                  'archived':archived, 'sortKey':'updated_at', 'useStateDbOnly':True}
        if cursor:
            text(cursor,4096); params['cursor'] = cursor
        result = self.call('thread/list',params)
        if not isinstance(result.get('data'),list) or len(result['data']) > limit:
            raise BridgeError('invalid Codex thread page')
        rows = []
        for row in result['data']:
            if not isinstance(row,dict):
                raise BridgeError('invalid Codex thread')
            text(row.get('id'),256)
            rows.append({key:row.get(key) for key in ('id','name','cwd','modelProvider','createdAt','updatedAt','status','source')})
        return {'data':rows, 'nextCursor':result.get('nextCursor'), 'statusScope':'connected-app-server'}

    def export(self, native_id, limit=8, budget=16000, cursor=None):
        text(native_id,256); number(limit,1,20); number(budget,2048,20000)
        info = self.call('thread/read', {'threadId':native_id, 'includeTurns':False}).get('thread')
        if not isinstance(info,dict) or info.get('id') != native_id:
            raise BridgeError('invalid Codex thread metadata')
        params = {'threadId':native_id, 'limit':limit, 'sortDirection':'desc', 'itemsView':'full'}
        if cursor is not None:
            text(cursor,4096); params['cursor'] = cursor
        page = self.call('thread/turns/list', params)
        if not isinstance(page.get('data'),list) or len(page['data']) > limit:
            raise BridgeError('invalid Codex turn page')
        value = {'version':1, 'contentKind':'native-chat-data', 'adapter':'codex', 'nativeId':native_id,
                 'title':info.get('name'), 'cwd':info.get('cwd'), 'turns':[],
                 'olderCursor':page.get('nextCursor'), 'omittedItems':0}
        for turn in page['data']:
            if not isinstance(turn,dict) or not isinstance(turn.get('items'),list):
                raise BridgeError('invalid Codex turn')
            selected = {'id':turn.get('id'), 'status':turn.get('status'), 'items':[]}
            value['turns'].append(selected)
            for item in turn['items']:
                if not isinstance(item,dict):
                    raise BridgeError('invalid Codex item')
                selected['items'].append(item)
                if len(encoded(value).encode('utf-8')) > budget:
                    selected['items'].pop(); value['omittedItems'] += 1
        value['turns'].reverse()
        if len(encoded(value).encode('utf-8')) > budget:
            raise BridgeError('metadata exceeds export budget')
        return value

    def close(self):
        self.selector.close()
        # Only the child/proxy owned by this bridge is stopped, never the daemon.
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=2)
        self.process.stdin.close(); self.process.stdout.close()

    def __enter__(self): return self
    def __exit__(self,*_): self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',default='codex')
    parser.add_argument('--socket')
    parser.add_argument('--state',help='optional hub state directory for import')
    parser.add_argument('--host',help='stable host identity, required for import')
    commands = parser.add_subparsers(dest='command',required=True)
    p = commands.add_parser('list'); p.add_argument('--limit',type=int,default=50); p.add_argument('--cursor'); p.add_argument('--archived',action='store_true')
    p = commands.add_parser('export'); p.add_argument('native_id'); p.add_argument('--limit',type=int,default=8); p.add_argument('--budget',type=int,default=16000); p.add_argument('--cursor'); p.add_argument('--context')
    args = parser.parse_args(argv)
    try:
        if args.state and not args.host:
            raise ValueError('--host is required for import')
        if getattr(args,'context',None) and not args.state:
            raise ValueError('--context requires --state')
        store = HubStore(args.state) if args.state else None
        if store:
            text(args.host,128)
        with CodexReader(args.binary,args.socket) as reader:
            if args.command == 'list':
                result = reader.list_chats(args.limit,args.cursor,args.archived)
                if store:
                    result['registered'] = [store.register_chat(args.host,'codex', row['id'], (row['name'] or '')[:160], row['cwd'] or '')['id'] for row in result['data']]
            else:
                result = reader.export(args.native_id,args.limit,args.budget,args.cursor)
                if store:
                    body = encoded(result)
                    if args.context and len(body) > 20000:
                        raise ValueError('import too large; reduce budget')
                    chat = store.register_chat(args.host,'codex',args.native_id,(result['title'] or '')[:160],result['cwd'] or '',args.context)
                    result['hubChatId'] = chat['id']
                    if args.context:
                        digest = hashlib.sha256(body.encode()).hexdigest()
                        entry_id = str(uuid.uuid5(uuid.NAMESPACE_URL, chat['id'] + ':' + digest))
                        entry = store.append(args.context,'note',body,'codex:' + chat['id'],entry_id)
                        result = {'chat':chat, 'entryId':entry['id'], 'omittedItems':result['omittedItems'], 'olderCursor':result['olderCursor']}
        print(encoded(result)); return 0
    except (OSError, ValueError, sqlite3.Error, BridgeError) as error:
        print(encoded({'error':type(error).__name__, 'message':str(error)}),file=sys.stderr); return 2


if __name__ == '__main__': raise SystemExit(main())
