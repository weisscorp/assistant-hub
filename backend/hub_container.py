"""Dependency-free container entrypoint. Mounted config may be a ConfigMap."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import urllib.request

from hub_network import bind_ip, http_origins, ProxyOrigin, hub_url


def private_write(path, data):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


def configuration(environ=None):
    env = os.environ if environ is None else environ
    data = Path(env.get('HUB_DATA_DIR', '/data'))
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime = Path(env.get('HUB_RUNTIME_DIR', '/tmp/assistant-hub'))
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    if runtime.stat().st_uid != os.getuid() or runtime.stat().st_mode & 0o077:
        raise ValueError('Runtime directory must be private and owned by the Hub user')
    config = {}
    supplied = env.get('HUB_CONFIG_FILE')
    if supplied:
        raw = Path(supplied).read_bytes()
        if len(raw) > 60000:
            raise ValueError('Mounted Hub config is too large')
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError('Mounted Hub config must be an object')
    secret = env.get('HUB_OWNER_TOKEN')
    if env.get('HUB_OWNER_TOKEN_FILE'):
        secret = Path(env['HUB_OWNER_TOKEN_FILE']).read_text().strip()
    if not secret or not 32 <= len(secret) <= 4096 or any(c.isspace() for c in secret):
        raise ValueError('Provide HUB_OWNER_TOKEN_FILE or HUB_OWNER_TOKEN (32..4096 characters)')
    token = runtime / 'owner-token'
    private_write(token, secret)
    host = env.get('HUB_HOST', 'auto')
    if host == 'auto':
        host = socket.gethostbyname(socket.gethostname())
    bind_ip(host)  # Bind the actual private interface, never a wildcard.
    port = int(env.get('HUB_PORT', '8778'))
    if not 1024 <= port <= 65535:
        raise ValueError('HUB_PORT must be 1024..65535')
    config.update(host=host, port=port, state=str(data / 'state'), token_file=str(token),
                  recordings_directory=str(data / 'recordings'))
    if 'HUB_HTTP_ORIGINS' in env:
        config['http_origins'] = [v.strip() for v in env['HUB_HTTP_ORIGINS'].split(',') if v.strip()]
    http_origins(config)
    if env.get('HUB_PUBLIC_URL'):
        config['public_url'] = env['HUB_PUBLIC_URL']
        config['trusted_proxies'] = [v.strip() for v in env.get('HUB_TRUSTED_PROXIES', '').split(',') if v.strip()]
    ProxyOrigin(config)
    if env.get('HUB_INCIDENT_TIMEOUT_SECONDS'):
        timeout = int(env['HUB_INCIDENT_TIMEOUT_SECONDS'])
        if not 15 <= timeout <= 600:
            raise ValueError('HUB_INCIDENT_TIMEOUT_SECONDS must be 15..600')
        config.setdefault('incidents', {})['timeout_seconds'] = timeout
    if 'providers' not in config:
        config['providers'] = {'ollama': {'type': 'ollama',
            'url': env.get('HUB_OLLAMA_URL', 'http://ollama:11434'),
            'model': env.get('HUB_OLLAMA_MODEL', 'qwen3:4b-instruct-2507-q4_K_M')}}
    if not isinstance(config['providers'], dict):
        raise ValueError('providers must be an object')
    path = runtime / 'config.json'
    private_write(path, json.dumps(config, ensure_ascii=False))
    return path


def healthcheck(environ=None):
    env = os.environ if environ is None else environ
    path = Path(env.get('HUB_RUNTIME_DIR', '/tmp/assistant-hub')) / 'config.json'
    config = json.loads(path.read_text())
    token = Path(config['token_file']).read_text().strip()
    request = urllib.request.Request(hub_url(config) + '/health', headers={'Authorization': 'Bearer ' + token})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=4) as reply:
        if reply.status != 200 or json.loads(reply.read(4096)).get('status') != 'ok':
            raise ValueError('Hub is not healthy')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['serve', 'healthcheck'], nargs='?', default='serve')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == 'healthcheck':
            healthcheck()
        else:
            path = configuration()
            os.execv(sys.executable, [sys.executable, str(Path(__file__).with_name('hub_server.py')), '--config', str(path)])
    except (OSError, ValueError, KeyError):
        # Neither input config nor credentials/URLs belong in Docker logs.
        print('Hub container configuration or health check failed; check mounted files and environment.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
