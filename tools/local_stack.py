#!/usr/bin/env python3
"""Setup/start/stop the local assistant stack. Never connects to a phone."""
import argparse
import hashlib
import json
import os
import plistlib
from pathlib import Path
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
import urllib.request
import urllib.parse
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
    if getattr(args,'dot_events',False):
        config['mcp_events'] = config.get('mcp_events') or {'request_timeout':300}
        config['providers'].setdefault('dot',{'type':'mcp_events','agent_id':'personal-dot'})
    mausbot_url = getattr(args, 'mausbot_url', None)
    mausbot_bot = getattr(args, 'mausbot_bot', None)
    mausbot_token = getattr(args, 'mausbot_token_file', None)
    if any((mausbot_url, mausbot_bot, mausbot_token)):
        mausbot = dict(config['providers'].get('mausbot', {'type': 'mausbot'}))
        if mausbot_url: mausbot['url'] = mausbot_url
        if mausbot_bot: mausbot['bot_id'] = mausbot_bot
        if mausbot_token: mausbot['token_file'] = str(mausbot_token.absolute())
        if not mausbot.get('url') or not mausbot.get('bot_id'):
            raise ValueError('MausBot requires --mausbot-url and --mausbot-bot')
        config['providers']['mausbot'] = mausbot
    if getattr(args, 'route_coding', False):
        if config['providers'].get('mausbot', {}).get('type') != 'mausbot':
            raise ValueError('Configure MausBot before enabling coding routing')
        config['providers']['auto'] = {'type': 'routing', 'general': 'ollama',
                                      'coding': 'mausbot', 'classifier': 'ollama'}
    binary=shutil.which('codex')
    if binary and 'codex' not in config['providers']:
        config['providers']['codex']={'type':'codex','binary':binary,'workspace':str(root/'agents/codex')}
    if getattr(args,'codex_link',False):
        executable=binary or config.get('providers',{}).get('codex',{}).get('binary')
        if not executable:raise ValueError('Install native Codex CLI before enabling the bridge')
        config.setdefault('codex_link',{'binary':executable,'host':'local','poll_seconds':15,'targets':'auto'})
    config.setdefault('coucou_capability',secrets.token_urlsafe(32))
    if args.local_node:
        config['local_node'] = True
    if getattr(args,'desktop_notifications',False):
        config.setdefault('desktop_notifications',{'id':'desktop:local-host','device':str(uuid.uuid4())})
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


def live(root,name,expected=None):
    try:
        path=root/(name+'.pid.json')
        if expected is not None:
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:return False
        saved=json.loads(path.read_text());pid=saved['pid']
        if type(pid) is not int or pid<2:return False
        if expected is not None and (saved.get('managed')!=expected or saved.get('match')!=expected['argv']):return False
        value=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True,stderr=subprocess.DEVNULL).strip()
        match=saved['match']
        if expected is not None:
            started=subprocess.check_output(['ps','-p',str(pid),'-o','lstart='],text=True,stderr=subprocess.DEVNULL).strip()
            return (value==' '.join(match) and started==saved.get('started') and os.getpgid(pid)==pid)
        # macOS executes the framework Python behind a Homebrew launcher path.
        owned=(all(part in value for part in match[1:]) if len(match)>1 and match[1].endswith('.py')
               else all(part in value for part in match))
        return owned and os.getpgid(pid)==pid
    except (OSError,ValueError,subprocess.CalledProcessError,KeyError,TypeError):return False


def launch(root,name,argv,environment=None,ownership=None):
    if live(root,name,ownership):return
    with (root/'logs'/(name+'.log')).open('ab') as log:
        p=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=environment,start_new_session=True)
    saved={'pid':p.pid,'match':argv}
    if ownership is not None:
        saved['managed']=ownership
        saved['started']=subprocess.check_output(['ps','-p',str(p.pid),'-o','lstart='],text=True,stderr=subprocess.DEVNULL).strip()
    write_private(root/(name+'.pid.json'),json.dumps(saved))


def owned_path(value,executable=False,directory=False):
    if not isinstance(value,str) or not Path(value).is_absolute():
        raise ValueError('Managed dependency paths must be absolute')
    path=Path(value)
    # Data stores must exist already: start never downloads or creates a model.
    # Executable symlinks are permitted only to owned, non-writable files.
    if directory and path.is_symlink():raise ValueError('Managed data directory cannot be a symlink')
    path=path.resolve(strict=True)
    info=path.stat()
    if info.st_uid!=os.getuid() or info.st_mode&(0o077 if directory else 0o022):
        raise ValueError('Managed paths must be owned and protected from other users')
    if directory:
        if not stat.S_ISDIR(info.st_mode):raise ValueError('Managed data path must be a private directory')
    elif not stat.S_ISREG(info.st_mode) or executable and not os.access(path,os.X_OK):
        raise ValueError('Managed runtime must be an owned executable/regular file')
    return str(path)


def private_config(root):
    root_info=root.lstat()
    path=root/'config.json';info=path.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid!=os.getuid() or root_info.st_mode&0o077 or
        not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>64000):
        raise ValueError('Stack configuration must be an owned private file in a private directory')
    return json.loads(path.read_text())


def managed_specs(config):
    """Validate opt-in services without adopting, installing or launching them."""
    result=[]
    for key,kind in (('managed_ollama','ollama'),('mausbot_runtime','mausbot')):
        value=config.get(key)
        if value is None:continue
        if not isinstance(value,dict):raise ValueError('Invalid managed runtime configuration')
        port=value.get('port')
        if type(port) is not int or not 1024<=port<=65535:raise ValueError('Managed port must be between 1024 and 65535')
        endpoints=[]
        for provider in config.get('providers',{}).values():
            if provider.get('type')!=kind:continue
            url=urllib.parse.urlsplit(provider.get('url',''))
            if (url.scheme=='http' and url.hostname=='127.0.0.1' and url.port==port and
                url.username is None and url.password is None and url.path in ('','/') and not url.query and not url.fragment):
                endpoints.append(url)
        if not endpoints:raise ValueError('Managed '+kind+' port must match a loopback provider URL')
        if kind=='ollama':
            binary=owned_path(value.get('binary'),executable=True)
            data=owned_path(value.get('models'),directory=True)
            argv=[binary,'serve']
            environment=dict(os.environ,OLLAMA_HOST='127.0.0.1:'+str(port),OLLAMA_MODELS=data)
        else:
            binary=owned_path(value.get('node'),executable=True)
            cli=owned_path(value.get('cli'))
            data=owned_path(value.get('data_dir'),directory=True)
            argv=[binary,cli,'serve','--local','--no-pair','--no-open','--port',str(port),'--data-dir',data]
            environment=None
        ownership={'argv':argv,'port':port,'data_dir':data}
        result.append({'name':kind,'argv':argv,'environment':environment,'ownership':ownership,'port':port})
    ports=[item['port'] for item in result]
    if len(ports)!=len(set(ports)) or config.get('port',8778) in ports:
        raise ValueError('Managed runtimes and Hub require separate ports')
    return result


def port_available(port):
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:probe.bind(('127.0.0.1',port))
        return True
    except OSError:return False


def start_managed(root,specs):
    # Preflight every dependency before launching any; a busy port never yields
    # a PID discovery/adoption or an overwritten foreign ownership file.
    pending=[]
    for spec in specs:
        if live(root,spec['name'],spec['ownership']):continue
        if live(root,spec['name']):raise RuntimeError('Managed '+spec['name']+' settings changed while its process is running')
        if not port_available(spec['port']):raise RuntimeError('Managed '+spec['name']+' port is occupied without matching ownership')
        pending.append(spec)
    for spec in pending:
        launch(root,spec['name'],spec['argv'],spec['environment'],ownership=spec['ownership'])
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            if not live(root,spec['name'],spec['ownership']):raise RuntimeError('Managed '+spec['name']+' exited before startup')
            with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:
                probe.settimeout(.2)
                if probe.connect_ex(('127.0.0.1',spec['port']))==0:break
            time.sleep(.1)
        else:raise RuntimeError('Managed '+spec['name']+' did not listen on its loopback port')


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


def ensure_coucou(root,config):
    coucou=Path(config.get('coucou_app',str(root/'runtime/coucou/Coucou.app')))/'Contents/MacOS/Coucou'
    if sys.platform=='darwin' and coucou.exists() and not live(root,'coucou'):
        # Do not duplicate or take ownership of an already running GUI app.
        running=subprocess.run(['pgrep','-f',str(coucou)],capture_output=True)
        if running.returncode!=0:
            domain='fr.louisraille.NotchBuddy'
            values={'soundEnabled':False,'chatProvider':'ollama','ollamaChatModel':'assistant-auto' if 'auto' in config['providers'] else 'assistant-ollama',
                    'ollamaServerURL':hub_url(config)+'/coucou/'+config['coucou_capability']}
            for key,item in values.items():
                subprocess.run(['defaults','write',domain,key,'-bool' if type(item) is bool else '-string',
                                'false' if item is False else item],check=True,stdout=subprocess.DEVNULL)
            launch_coucou(root,coucou.parent.parent.parent)


def start(root):
    config=private_config(root)
    # Bring up the GUI independently of slow/cold background dependencies.
    ensure_coucou(root,config)
    ao=root/'runtime/ao'
    dependencies=managed_specs(config)
    if ao.exists() and any(item['port']==3001 for item in dependencies):
        raise ValueError('Managed runtime port conflicts with the configured AO daemon')
    start_managed(root,dependencies)
    if ao.exists():
        env=dict(os.environ,AO_DATA_DIR=str(root/'ao-data'),AO_RUN_FILE=str(root/'ao-running.json'),AO_PORT='3001')
        launch(root,'ao',[str(ao),'daemon'],env)
    if not live(root,'hub'):
        try:
            hub_request(root,'/health')
        except OSError:pass
        else:raise RuntimeError('A Hub already owns this port without a matching PID. Do not replace its ownership file.')
    launch(root,'hub',[sys.executable,str(BACKEND/'hub_server.py'),'--config',str(root/'config.json')])
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            value=hub_request(root,'/health')
            if config.get('local_node'):
                launch(root,'local-node',[sys.executable,str(BACKEND/'hub_node.py'),
                    '--url',hub_url(config),'--token-file',str(root/'token'),
                    '--id','local-host','--label','Local computer','--kind','computer'])
            desktop=config.get('desktop_notifications')
            if desktop and sys.platform=='darwin':
                argv=[sys.executable,str(BACKEND/'hub_desktop.py'),'--url',hub_url(config),
                    '--token-file',str(root/'token'),'--id',desktop['id'],'--device',desktop['device']]
                if config.get('coucou_socket'):argv+=['--coucou-socket',config['coucou_socket']]
                launch(root,'desktop-notifications',argv)
            print(json.dumps(value));return
        except (OSError,ValueError):time.sleep(.2)
    raise RuntimeError('Hub did not start. Read '+str(root/'logs/hub.log'))


def stop_process(root,name,ownership=None):
    if live(root,name,ownership):
        saved=json.loads((root/(name+'.pid.json')).read_text());os.killpg(saved['pid'],signal.SIGTERM)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and live(root,name,ownership):time.sleep(.1)
        if live(root,name,ownership):os.killpg(saved['pid'],signal.SIGKILL)
        print(name+' stopped')


def stop(root):
    config=private_config(root)
    for name in ('desktop-notifications','local-node','hub','ao','coucou'):
        stop_process(root,name)
    for spec in reversed(managed_specs(config)):
        stop_process(root,spec['name'],spec['ownership'])


def login_start(root,enabled):
    """One-shot GUI login startup; no recording, timers or respawn loop."""
    if sys.platform!='darwin':raise ValueError('Login startup requires macOS')
    root=Path(root).absolute();private_config(root)
    label='local.assistant.stack.login'
    agents=Path.home()/'Library/LaunchAgents';agents.mkdir(parents=True,exist_ok=True)
    target=agents/(label+'.plist');domain='gui/'+str(os.getuid())
    if target.exists():
        previous=plistlib.loads(target.read_bytes())
        if previous.get('Label')!=label:raise ValueError('Unexpected login agent; refusing to replace')
        backups=root/'backups';backups.mkdir(parents=True,exist_ok=True,mode=0o700)
        shutil.copy2(target,backups/(label+'-'+str(time.time_ns())+'.plist'))
    if not enabled:
        subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
        target.unlink(missing_ok=True)
        print('Assistant login startup disabled; running applications are unchanged')
        return
    job={'Label':label,'ProgramArguments':[sys.executable,str(Path(__file__).resolve()),'--directory',str(root),'start'],
         'RunAtLoad':True,'LimitLoadToSessionType':'Aqua','ProcessType':'Interactive',
         'WorkingDirectory':str(PROJECT),'EnvironmentVariables':{'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
         'StandardOutPath':str(root/'logs/login-start.log'),'StandardErrorPath':str(root/'logs/login-start-error.log')}
    (root/'logs').mkdir(parents=True,exist_ok=True,mode=0o700)
    write_private(target,plistlib.dumps(job).decode())
    subprocess.run(['plutil','-lint',str(target)],check=True)
    subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
    subprocess.run(['launchctl','enable',domain+'/'+label],check=True)
    subprocess.run(['launchctl','bootstrap',domain,str(target)],check=True)
    print('Assistant login startup enabled (Coucou and its configured local stack)')


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,default=DEFAULT)
    sub=p.add_subparsers(dest='command',required=True)
    setup=sub.add_parser('setup');setup.add_argument('--hermes-root',type=Path);setup.add_argument('--hermes-python',type=Path)
    setup.add_argument('--dot-events',action='store_true',help='Enable the asynchronous OpenAI dot MCP Events provider; requires a plugin subscription')
    setup.add_argument('--mausbot-url',help='URL of an existing owner-configured MausBot server')
    setup.add_argument('--mausbot-bot',help='ID of the MausBot coding bot')
    setup.add_argument('--mausbot-token-file',type=Path,help='Owned private MausBot token file; optional on loopback')
    setup.add_argument('--route-coding',action='store_true',help='Route coding requests to configured MausBot using the local Ollama classifier')
    setup.add_argument('--local-node',action='store_true',help='Enable a local node with read-only system.info/system.time')
    setup.add_argument('--desktop-notifications',action='store_true',help='Display incident reports in the Coucou mascot area')
    setup.add_argument('--codex-link',action='store_true',help='Observe native Codex completions and enable explicit voice continuations')
    for name in ('start','stop','status','open','coucou-url','autostart-enable','autostart-disable'):sub.add_parser(name)
    ask=sub.add_parser('ask');ask.add_argument('prompt');ask.add_argument('--provider');ask.add_argument('--context')
    args=p.parse_args();root=args.directory.absolute()
    if args.command=='setup':configure(args)
    elif args.command=='autostart-enable':login_start(root,True)
    elif args.command=='autostart-disable':login_start(root,False)
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
        provider=args.provider or ('auto' if 'auto' in info['providers'] else 'ollama')
        value=hub_request(root,'/api/messages',{'id':str(uuid.uuid4()),'context_id':context,'client':'console','provider':provider,'prompt':args.prompt})
        print('Task: '+value['id'],flush=True)
        while value['status'] not in {'completed','failed','cancelled','unknown','needs_user'}:
            time.sleep(.5);value=hub_request(root,'/api/tasks/'+value['id'])
        print(value['output'] if value['status']=='completed' else value['error'])
        return 0 if value['status']=='completed' else 1
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as error:print(type(error).__name__+': '+str(error),file=sys.stderr);sys.exit(1)
