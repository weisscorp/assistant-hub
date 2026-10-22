#!/usr/bin/env python3
"""Temporary, peer-restricted image transfer inside the owner's private network."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--bind', required=True)
    parser.add_argument('--peer', required=True)
    parser.add_argument('--port', type=int, default=18778)
    parser.add_argument('--lifetime', type=int, default=1800)
    parser.add_argument('--metadata', type=Path, required=True)
    args = parser.parse_args()
    nonce = secrets.token_urlsafe(24)
    paths = {'/' + nonce + '/' + name: args.directory / name for name in ('images.tar.gz', 'install.py')}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            path = paths.get(self.path)
            if self.client_address[0] != args.peer or path is None or not path.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(path.stat().st_size))
            self.send_header('Content-Type', 'application/octet-stream')
            self.end_headers()
            with path.open('rb') as stream:
                while chunk := stream.read(1048576):
                    self.wfile.write(chunk)
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.timeout = 1
    args.metadata.write_text(json.dumps({'base_url': 'http://' + args.bind + ':' + str(args.port) + '/' + nonce}))
    args.metadata.chmod(0o600)
    print('Private transfer ready for the selected NAS; URLs and requests are not logged.', flush=True)
    deadline = time.monotonic() + args.lifetime
    try:
        while time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
