#!/usr/bin/env python3
"""Create/list/revoke owner API keys locally; never print a secret."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'backend'))
from agent_hub import HubStore
from hub_auth import AccessKeys
from hub_server import private_json


def create(keys, name, output, scope='owner'):
    # Refuse overwrites and symlinks. The secret goes only to a new private file,
    # never stdout, process arguments, a URL or the Hub's event/history tables.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    row = None
    try:
        with os.fdopen(fd, 'w') as stream:
            row, secret = keys.issue(name,scope)
            stream.write(secret+'\n')
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if row:
            keys.revoke(row['id'])
        Path(output).unlink()
        raise
    return {**row, 'key_file':str(Path(output).absolute())}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home()/'.local/share/assistant-stack/config.json')
    sub = parser.add_subparsers(dest='command', required=True)
    make = sub.add_parser('create')
    make.add_argument('name')
    make.add_argument('--output', type=Path, required=True)
    make.add_argument('--scope',choices=['owner','webhook','mcp'],default='owner',help='webhook: event producer; mcp: dot request tools only; owner: full API')
    sub.add_parser('list')
    revoke = sub.add_parser('revoke')
    revoke.add_argument('id', help='Public UUID from list; not the secret')
    args = parser.parse_args()
    keys = AccessKeys(HubStore(private_json(args.config)['state']))
    if args.command == 'create':
        value = create(keys, args.name, args.output,args.scope)
    elif args.command == 'list':
        value = keys.list()
    else:
        keys.revoke(args.id)
        value = {'id':args.id, 'revoked':True}
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as error:
        print(type(error).__name__+': '+str(error), file=sys.stderr)
        sys.exit(1)
