#!/usr/bin/env python3
"""Authenticated stdio relay for a private dot MCP tunnel. No device access.

Forward JSON-RPC only to the configured Hub's /mcp endpoint. The dedicated MCP
key stays in an owned private file; stdout contains only protocol responses.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import urllib.error
import urllib.request
from hub_auth import digest
from hub_network import hub_url
from hub_server import private_json


def run(config, token_file, source, output):
    info = token_file.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > 8192):
        raise ValueError('Use an owned private MCP key file')
    token = token_file.read_text().strip()
    if digest(token) is None:
        raise ValueError('Invalid MCP key')
    url = hub_url(config) + '/mcp'
    # Never send a private Hub key through environment-configured HTTP proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while True:
        raw = source.readline(65537)
        if not raw:
            return
        if len(raw) > 65536 or not raw.endswith(b'\n'):
            raise ValueError('MCP message exceeded budget')
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError('Expected a JSON-RPC object')
        request = urllib.request.Request(url, raw, {'Authorization':'Bearer '+token, 'Content-Type':'application/json'})
        try:
            with opener.open(request, timeout=30) as response:
                result = response.read(65537)
                if len(result) > 65536:
                    raise ValueError('MCP response exceeded budget')
                if not result:
                    continue
                value = json.loads(result)
        except (OSError, ValueError):
            if 'id' not in body:
                continue
            value = {'jsonrpc':'2.0','id':body['id'],'error':{'code':-32000,'message':'Hub MCP unavailable; check private configuration and key scope'}}
        output.write((json.dumps(value, ensure_ascii=False, separators=(',',':'))+'\n').encode())
        output.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    args = parser.parse_args()
    try:
        run(private_json(args.config), args.token_file, sys.stdin.buffer, sys.stdout.buffer)
    except Exception:
        print('Dot MCP relay stopped; inspect private setup.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
