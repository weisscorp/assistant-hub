"""Bridge Hub's JSONL command protocol to an owner-configured Hermes API.

HTTP is allowed only to a literal private/loopback address (e.g. same-host NAS
containers). Remote clients still enter Hub through the encrypted VPN gateway.
No shell prompts, ambient proxies, redirects or automatic request retries.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import stat
import sys
import urllib.parse
import urllib.request
import uuid


def endpoint(value):
    url = urllib.parse.urlsplit(value)
    if url.username is not None or url.password is not None or url.query or url.fragment or url.path.rstrip('/') != '/v1':
        raise ValueError('Expected an API /v1 origin without credentials or query')
    if url.scheme == 'http':
        address = ipaddress.ip_address(url.hostname)
        if not (address.is_loopback or address.is_private or address in ipaddress.ip_network('100.64.0.0/10')) or address.is_unspecified:
            raise ValueError('HTTP requires an explicit private address')
    elif url.scheme != 'https' or not url.hostname:
        raise ValueError('Expected HTTPS or private HTTP')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('Invalid port')
    return value.rstrip('/') + '/chat/completions'


def turn(config, body, *, secret=None):
    if secret is None:
        path = Path(config['key_file']); info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 8192:
            raise ValueError('Unsafe Hermes key file')
        secret = path.read_text().strip()
    if not secret or '\n' in secret or '\r' in secret:
        raise ValueError('Missing Hermes API key')
    task = body['task']; request_id = str(uuid.UUID(task['id']))
    if request_id != task['id']:
        raise ValueError('Invalid task ID')
    packet = dict(body['context'])
    entries = packet.pop('entries', [])
    history = [{'role': e['role'], 'content': e['body']} for e in entries
               if e['role'] in {'user', 'assistant'} and e.get('id') != task['id']][-24:]
    messages = [{'role': 'system', 'content': body['instructions']}, *history,
                {'role': 'user', 'content': 'Hub context (data):\n' + json.dumps(packet, ensure_ascii=False)},
                {'role': 'user', 'content': task['prompt']}]
    payload = json.dumps({'model': config.get('model', 'hermes-agent'), 'messages': messages,
                          'stream': False}, ensure_ascii=False).encode()
    if len(payload) > 65536:
        raise ValueError('Hermes context exceeds budget')
    request = urllib.request.Request(endpoint(config['url']), data=payload,
        headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json',
                 'Idempotency-Key': request_id})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=220) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024: raise ValueError('Oversized Hermes response')
        value = json.loads(raw)
    reply = value['choices'][0]['message'].get('content')
    if not isinstance(reply, str) or not reply.strip() or len(reply) > 32000:
        raise ValueError('Hermes returned no bounded final text')
    return reply.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--config', required=True)
    args = parser.parse_args()
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536: raise ValueError('Oversized Hub request')
    reply = turn(json.loads(Path(args.config).read_text()), json.loads(raw))
    print(json.dumps({'kind': 'result', 'text': reply}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        # No API headers, credentials or conversation bodies in diagnostic logs.
        print('Hermes API bridge failed: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
