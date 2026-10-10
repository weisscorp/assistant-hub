#!/usr/bin/env python3
"""Setup/start/stop the local assistant stack. Never connects to a phone."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser

PROJECT=Path(__file__).resolve().parent.parent
BACKEND=PROJECT/'backend'
DEFAULT=Path.home()/'.local/share/assistant-stack'
sys.path.insert(0,str(BACKEND))
from hub_network import hub_url


def write_private(path,value):
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as stream: stream.write(value)
    Path(path).chmod(0o600)


def configure(args):
    root=args.directory.absolute()
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if root.is_symlink() or root.stat().st_uid!=os.getuid() or root.stat().st_mode&0o077:
        raise ValueError('Use an owned private directory (0700)')
    for name in ('state','logs','runtime','agents','agents/codex','agents/hermes','hermes-home','ao-data'):
        (root/name).mkdir(mode=0o700,parents=True,exist_ok=True)
    if not (root/'token').exists():write_private(root/'token',secrets.token_urlsafe(48)+'\n')
    config_path=root/'config.json'
    config=json.loads(config_path.read_text()) if config_path.exists() else {
        'host':'127.0.0.1','port':8778,'state':str(root/'state'),'token_file':str(root/'token'),
        'coucou_socket':str(Path.home()/'Library/Application Support/NotchBuddy/nb.sock'),
        'providers':{'ollama':{'type':'ollama','url':'http://127.0.0.1:11434','model':'qwen3:4b-instruct-2507-q4_K_M'}}}
    binary=shutil.which('codex')
    if binary and 'codex' not in config['providers']:
        config['providers']['codex']={'type':'codex','binary':binary,'workspace':str(root/'agents/codex')}
    config.setdefault('coucou_capability',secrets.token_urlsafe(32))
    if args.local_node:
        config['local_node'] = True
    config['providers'].setdefault('openai',{'type':'cloud','protocol':'openai','url':'https://api.openai.com/v1',
        'model':os.environ.get('OPENAI_MODEL',''),'key_env':'OPENAI_API_KEY'})
    config['providers'].setdefault('anthropic',{'type':'cloud','protocol':'anthropic','url':'https://api.anthropic.com/v1',
        'model':os.environ.get('ANTHROPIC_MODEL',''),'key_env':'ANTHROPIC_API_KEY'})
    if (root/'runtime/ao').exists():
        config['providers']['orchestrator']={'type':'orchestrator','url':'http://127.0.0.1:3001',
            'hub_url':hub_url(config),'token_file':str(root/'token'),
            'mcp_python':str(PROJECT/'verification/home-mcp-venv/bin/python'),'mcp_client':str(BACKEND/'hub_mcp_client.py')}
    if args.hermes_root and args.hermes_python:
        hermes_root=args.hermes_root.resolve(strict=True); python=args.hermes_python.absolute()
        revision=subprocess.check_output(['git','-C',str(hermes_root),'rev-parse','HEAD'],text=True).strip()
        hermes={'root':str(hermes_root),'revision':revision,'home':str(root/'hermes-home'),
                'workspace':str(root/'agents/hermes'),'model':config['providers']['ollama']['model'],
                'base_url':config['providers']['ollama']['url']+'/v1','hub_url':hub_url(config),
                'token_file':str(root/'token'),'mcp_script':str(BACKEND/'hub_mcp.py'),
                'mcp_python':str(PROJECT/'verification/home-mcp-venv/bin/python')}
        write_private(root/'hermes.json',json.dumps(hermes,indent=2))
        # The bounded Hub tool catalog does not need progressive tool search.
        # Keep unrelated settings in this isolated profile intact.
        subprocess.run([str(python),'-c',
            'from pathlib import Path; from ruamel.yaml import YAML; import sys; '
            'p=Path(sys.argv[1]); y=YAML(); d=y.load(p.read_text()) if p.exists() else {}; '
            'd=d or {}; d.setdefault("tools",{}).setdefault("tool_search",{})["enabled"]="off"; '
            'f=p.open("w"); y.dump(d,f); f.close(); p.chmod(0o600)',
            str(root/'hermes-home/config.yaml')],check=True)
        config['providers']['hermes']={'type':'command','argv':[str(python),str(BACKEND/'hub_hermes.py'),'--config',str(root/'hermes.json')],
                                      'label':'Hermes · локальная Ollama + общая память MCP','log_file':str(root/'logs/hermes.log')}
    write_private(config_path,json.dumps(config,indent=2))
    print('Configuration: '+str(config_path))
    return config


def hub_request(root,path,body=None):
    config=json.loads((root/'config.json').read_text()); token=(root/'token').read_text().strip()
    request=urllib.request.Request(hub_url(config)+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=15) as response:return json.load(response)


def live(root,name):
    try:
        saved=json.loads((root/(name+'.pid.json')).read_text());pid=saved['pid']
        if type(pid) is not int or pid<2:return False
        value=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True,stderr=subprocess.DEVNULL).strip()
        match=saved['match']
        # macOS executes the framework Python behind a Homebrew launcher path.
        owned=(all(part in value for part in match[1:]) if len(match)>1 and match[1].endswith('.py')
               else all(part in value for part in match))
        return owned and os.getpgid(pid)==pid
    except (FileNotFoundError,ProcessLookupError,subprocess.CalledProcessError,KeyError):return False


def launch(root,name,argv,environment=None):
    if live(root,name):return
    with (root/'logs'/(name+'.log')).open('ab') as log:
        p=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=environment,start_new_session=True)
    write_private(root/(name+'.pid.json'),json.dumps({'pid':p.pid,'match':argv}))


def launch_coucou(root,app):
    executable=str(app/'Contents/MacOS/Coucou')
    # Register the GUI launch with macOS so Spotlight/Dock reuse this instance.
    subprocess.run(['open','-a',str(app)],check=True)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        running=subprocess.run(['pgrep','-f',executable],capture_output=True)
        owned=[]
        for value in running.stdout.split():
            pid=int(value)
            try:
                command=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True).strip()
                if command==executable and os.getpgid(pid)==pid:owned.append(pid)
            except (ProcessLookupError,subprocess.CalledProcessError):pass
        if len(owned)==1:
            write_private(root/'coucou.pid.json',json.dumps({'pid':owned[0],'match':[executable]}))
            return
        time.sleep(.1)
    raise RuntimeError('Could not identify the Coucou instance launched by macOS')


def start(root):
    config=json.loads((root/'config.json').read_text())
    ao=root/'runtime/ao'
    if ao.exists():
        env=dict(os.environ,AO_DATA_DIR=str(root/'ao-data'),AO_RUN_FILE=str(root/'ao-running.json'),AO_PORT='3001')
        launch(root,'ao',[str(ao),'daemon'],env)
    if not live(root,'hub'):
        try:
            hub_request(root,'/health')
        except OSError:pass
        else:raise RuntimeError('A Hub already owns this port without a matching PID. Do not replace its ownership file.')
    launch(root,'hub',[sys.executable,str(BACKEND/'hub_server.py'),'--config',str(root/'config.json')])
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        try:
            value=hub_request(root,'/health')
            if config.get('local_node'):
                launch(root,'local-node',[sys.executable,str(BACKEND/'hub_node.py'),
                    '--url',hub_url(config),'--token-file',str(root/'token'),
                    '--id','local-host','--label','Local computer','--kind','computer'])
            coucou=Path(config.get('coucou_app',str(root/'runtime/coucou/Coucou.app')))/'Contents/MacOS/Coucou'
            if sys.platform=='darwin' and coucou.exists() and not live(root,'coucou'):
                # Do not duplicate or take ownership of an already running GUI app.
                running=subprocess.run(['pgrep','-f',str(coucou)],capture_output=True)
                if running.returncode!=0:
                    domain='fr.louisraille.NotchBuddy'
                    values={'soundEnabled':False,'chatProvider':'ollama','ollamaChatModel':'assistant-ollama',
                            'ollamaServerURL':hub_url(config)+'/coucou/'+config['coucou_capability']}
                    for key,item in values.items():
                        subprocess.run(['defaults','write',domain,key,'-bool' if type(item) is bool else '-string',
                                        'false' if item is False else item],check=True,stdout=subprocess.DEVNULL)
                    launch_coucou(root,coucou.parent.parent.parent)
            print(json.dumps(value));return
        except (OSError,ValueError):time.sleep(.2)
    raise RuntimeError('Hub did not start. Read '+str(root/'logs/hub.log'))


def stop(root):
    for name in ('local-node','hub','ao','coucou'):
        if live(root,name):
            saved=json.loads((root/(name+'.pid.json')).read_text());os.killpg(saved['pid'],signal.SIGTERM)
            deadline=time.monotonic()+8
            while time.monotonic()<deadline and live(root,name):time.sleep(.1)
            if live(root,name):os.killpg(saved['pid'],signal.SIGKILL)
            print(name+' stopped')


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,default=DEFAULT)
    sub=p.add_subparsers(dest='command',required=True)
    setup=sub.add_parser('setup');setup.add_argument('--hermes-root',type=Path);setup.add_argument('--hermes-python',type=Path)
    setup.add_argument('--local-node',action='store_true',help='Enable a local node with read-only system.info/system.time')
    for name in ('start','stop','status','open','coucou-url'):sub.add_parser(name)
    ask=sub.add_parser('ask');ask.add_argument('prompt');ask.add_argument('--provider',default='ollama');ask.add_argument('--context')
    args=p.parse_args();root=args.directory.absolute()
    if args.command=='setup':configure(args)
    elif args.command=='start':start(root)
    elif args.command=='stop':stop(root)
    elif args.command=='status':
        try:
            value=hub_request(root,'/api/state?client=console')
            print(json.dumps({'hub':True,'ao':live(root,'ao'),'providers':value['providers'],'contexts':value['contexts']},ensure_ascii=False,indent=2))
        except OSError:print(json.dumps({'hub':False,'ao':live(root,'ao')}));return 1
    elif args.command=='coucou-url':
        # Display only on an explicit owner request, never in startup/status logs.
        config=json.loads((root/'config.json').read_text())
        print(hub_url(config)+'/coucou/'+config['coucou_capability'])
    elif args.command=='open':
        ticket=hub_request(root,'/api/tickets',{})
        config=json.loads((root/'config.json').read_text())
        webbrowser.open(hub_url(config)+ticket['path'])
        print('Opened Assistant Hub')
    else:
        info=hub_request(root,'/api/state?client=console');context=args.context or info['focus']['context_id']
        import uuid
        value=hub_request(root,'/api/messages',{'id':str(uuid.uuid4()),'context_id':context,'client':'console','provider':args.provider,'prompt':args.prompt})
        print('Task: '+value['id'],flush=True)
        while value['status'] not in {'completed','failed','cancelled','unknown'}:
            time.sleep(.5);value=hub_request(root,'/api/tasks/'+value['id'])
        print(value['output'] if value['status']=='completed' else value['error'])
        return 0 if value['status']=='completed' else 1
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as error:print(type(error).__name__+': '+str(error),file=sys.stderr);sys.exit(1)
