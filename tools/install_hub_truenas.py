#!/usr/bin/env python3
"""Install a tested local image bundle through TrueNAS middleware, without SSH keys.

Run as root in the owner's authenticated TrueNAS Shell. This creates only the
named dataset and app, refuses existing app/state, and never prints credentials.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.request


def command(*args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Command failed: ' + args[0] + '\n' + result.stderr[-2000:])
    return result.stdout


def middleware(method, *args):
    return json.loads(command('midclt', 'call', method, *(json.dumps(a) for a in args)))


def private_file(path, value, uid=10001, gid=10001):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)
        os.fchown(stream.fileno(), uid, gid)


def compose(root, image, collector_image, nas_ip, nas_name, port, ollama_url, model):
    restrictions = {'restart': 'unless-stopped', 'init': True, 'read_only': True,
        'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true']}
    return {'services': {
        'hub': {**restrictions, 'image': image, 'pull_policy': 'never',
            'ports': [nas_ip + ':' + str(port) + ':8778'],
            'environment': {'HUB_OWNER_TOKEN_FILE': '/run/secrets/owner-token',
                'HUB_HTTP_ORIGINS': 'http://' + nas_name + ':' + str(port) + ',http://' + nas_ip + ':' + str(port) + ',http://hub:8778',
                'HUB_OLLAMA_URL': ollama_url, 'HUB_OLLAMA_MODEL': model, 'HUB_INCIDENT_TIMEOUT_SECONDS': '300'},
            'volumes': [str(root / 'hub') + ':/data', str(root / 'secrets/owner-token') + ':/run/secrets/owner-token:ro'],
            'tmpfs': ['/tmp:rw,nosuid,nodev,size=64m,mode=1777'], 'stop_grace_period': '30s'},
        'collectors': {**restrictions, 'image': collector_image, 'pull_policy': 'never',
            'depends_on': {'hub': {'condition': 'service_healthy'}},
            'volumes': [str(root / 'collectors') + ':/data', str(root / 'config') + ':/config:ro',
                str(root / 'secrets/collector-token') + ':/run/secrets/collector-token:ro', str(root / 'plugins') + ':/plugins:ro'],
            'tmpfs': ['/tmp:rw,nosuid,nodev,size=16m,mode=1777'], 'stop_grace_period': '330s'}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle-url', required=True)
    parser.add_argument('--bundle-sha256', required=True)
    parser.add_argument('--dataset', default='nvme/assistant-hub')
    parser.add_argument('--app', default='assistant-hub')
    parser.add_argument('--image', default='assistant-hub:dev-20261021.2')
    parser.add_argument('--collector-image', default='assistant-hub-collectors:dev-20261021')
    parser.add_argument('--nas-ip', required=True)
    parser.add_argument('--nas-name', required=True)
    parser.add_argument('--port', type=int, default=8778)
    parser.add_argument('--ollama-url', required=True)
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run inside the authenticated TrueNAS root Shell')
    if middleware('app.query', [['name', '=', args.app]]):
        raise SystemExit('The application already exists; refusing to replace it')
    root = Path('/mnt') / args.dataset
    if root.exists():
        raise SystemExit('Deployment directory already exists; refusing to replace it')
    # Check the existing model before modifying any persistent state.
    with urllib.request.urlopen(args.ollama_url.rstrip('/') + '/api/tags', timeout=10) as response:
        models = [m['name'] for m in json.load(response).get('models', [])]
    if args.model not in models:
        raise SystemExit('The selected model is not installed on the configured Ollama')
    middleware('pool.dataset.create', {'name': args.dataset, 'type': 'FILESYSTEM', 'compression': 'LZ4', 'atime': 'OFF'})
    os.chmod(root, 0o700)
    for name in ('hub', 'collectors', 'config', 'plugins', 'secrets'):
        directory = root / name
        directory.mkdir(mode=0o700)
        os.chown(directory, 10001, 10001)
    releases = root / 'releases'
    releases.mkdir(mode=0o700)
    archive = releases / 'images.tar.gz'
    digest = hashlib.sha256()
    print('Downloading the tested Docker images', flush=True)
    with urllib.request.urlopen(args.bundle_url, timeout=30) as response, archive.open('xb') as stream:
        while chunk := response.read(1048576):
            digest.update(chunk)
            stream.write(chunk)
    if digest.hexdigest() != args.bundle_sha256:
        raise SystemExit('Image archive checksum mismatch; no images were loaded')
    print(command('docker', 'load', '-i', str(archive), timeout=300), flush=True)
    for image in (args.image, args.collector_image):
        observed = command('docker', 'image', 'inspect', '--format', '{{.Architecture}}', image).strip()
        if observed != 'amd64':
            raise SystemExit('Expected tested linux/amd64 images')
    private_file(root / 'secrets/owner-token', secrets.token_urlsafe(48) + '\n')
    bootstrap = """from pathlib import Path
import os
from agent_hub import HubStore
from hub_auth import AccessKeys
os.umask(0o077)
keys=AccessKeys(HubStore('/data/state'))
row,value=keys.issue('scheduled-collectors','webhook')
fd=os.open('/secrets/collector-token',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as stream:stream.write(value+'\\n')
print('Producer key created with webhook scope')
"""
    print(command('docker', 'run', '--rm', '--network', 'none', '-v', str(root / 'hub') + ':/data',
        '-v', str(root / 'secrets') + ':/secrets', '--entrypoint', 'python3', args.image, '-c', bootstrap), flush=True)
    collector_config = '''[hub]
url = "http://hub:8778"
token_file = "/run/secrets/collector-token"
[runtime]
state = "/data/collectors"
workers = 4
max_pending = 1000
retention_days = 7
[[sources]]
id = "ollama-health"
title = "Доступность Ollama на TrueNAS"
plugin = "hub_collector_plugins:HttpStatus"
interval_seconds = 60
timeout_seconds = 15
[sources.options]
url = %s
request_timeout_seconds = 10
''' % json.dumps(args.ollama_url.rstrip('/') + '/api/tags')
    private_file(root / 'config/collectors.toml', collector_config)
    plan = compose(root, args.image, args.collector_image, args.nas_ip, args.nas_name,
                   args.port, args.ollama_url, args.model)
    private_file(root / 'compose.json', json.dumps(plan, indent=2), uid=0, gid=0)
    print('Creating the TrueNAS application through app.create', flush=True)
    job_id = middleware('app.create', {'app_name': args.app, 'custom_app': True, 'custom_compose_config': plan})
    private_file(root / 'installation.json', json.dumps({'app': args.app, 'dataset': args.dataset,
        'job_id': job_id, 'images_sha256': digest.hexdigest(), 'images': {
            image: command('docker', 'image', 'inspect', '--format', '{{.Id}}', image).strip()
            for image in (args.image, args.collector_image)}, 'ollama_url': args.ollama_url,
        'model': args.model, 'installed_at': int(time.time())}, indent=2), uid=0, gid=0)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        jobs = middleware('core.get_jobs', [['id', '=', job_id]])
        if jobs and jobs[0]['state'] == 'SUCCESS':
            print(json.dumps({'installed': True, 'app': args.app, 'dataset': args.dataset,
                'url': 'http://' + args.nas_name + ':' + str(args.port), 'job_id': job_id}), flush=True)
            return
        if jobs and jobs[0]['state'] in {'FAILED', 'ABORTED'}:
            raise RuntimeError('TrueNAS app job failed: ' + str(jobs[0].get('error')))
        time.sleep(2)
    raise RuntimeError('TrueNAS application job is still pending; inspect its job ID')


if __name__ == '__main__':
    main()
