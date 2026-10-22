"""Collector contract and built-ins. Only owner-installed configuration loads code."""
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


@dataclass
class Observation:
    data: object
    key: str = 'snapshot'
    kind: str = 'snapshot'


@dataclass
class Batch:
    events: list[Observation] = field(default_factory=list)
    cursor: object = None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None  # Never forward a source/Hub token to a redirect destination.


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def secret(options, prefix='token'):
    env, path = options.get(prefix + '_env'), options.get(prefix + '_file')
    if env and path:
        raise ValueError('Choose a secret environment variable or file')
    if env:
        value = os.environ[env]
    elif path:
        with Path(path).open() as stream:
            value = stream.read(8193)
    else:
        return None
    value = value.strip()
    if not 1 <= len(value) <= 8192 or '\r' in value or '\n' in value:
        raise ValueError('Invalid secret')
    return value


def endpoint(value):
    if not isinstance(value, str):
        raise ValueError('Expected HTTP URL')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or any(ord(c) < 33 for c in value)):
        raise ValueError('Expected an HTTP(S) URL without credentials or fragment')
    parsed.port
    return value


def pointer(value, path):
    """RFC 6901 pointer. No eval, expressions, templates or executable payloads."""
    if path == '':
        return value
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('JSON pointer must start with /')
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


class HttpJson:
    """Read a JSON API and select a value or a bounded list of keyed items."""
    def __init__(self, options):
        self.options = options
        self.url = endpoint(options['url'])
        self.timeout = options.get('request_timeout_seconds', 10)
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= 120:
            raise ValueError('Invalid HTTP timeout')

    def request(self):
        headers = {'Accept': 'application/json', 'User-Agent': 'AssistantHub-Collector/1'}
        for name, env in self.options.get('headers_env', {}).items():
            if name.lower() in {'host', 'content-length', 'transfer-encoding'}:
                raise ValueError('Invalid source header')
            headers[name] = os.environ[env]
        token = secret(self.options)
        if token:
            headers['Authorization'] = 'Bearer ' + token
        return urllib.request.Request(self.url, headers=headers)

    def collect(self, *, cursor, now):
        limit = self.options.get('max_response_bytes', 262144)
        if type(limit) is not int or not 1 <= limit <= 1048576:
            raise ValueError('Invalid response limit')
        with opener().open(self.request(), timeout=self.timeout) as reply:
            raw = reply.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Source response is too large')
        data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
        value = pointer(data, self.options.get('pointer', ''))
        if not self.options.get('items', False):
            return Batch([Observation(value)], cursor)
        if not isinstance(value, list) or len(value) > 256:
            raise ValueError('Expected at most 256 source items')
        id_pointer = self.options.get('id_pointer', '/id')
        return Batch([Observation(item, str(pointer(item, id_pointer))) for item in value], cursor)


class HttpStatus(HttpJson):
    """Report reachability/status changes without copying a response body."""
    def collect(self, *, cursor, now):
        try:
            with opener().open(self.request(), timeout=self.timeout) as reply:
                code = reply.status
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
        except (urllib.error.URLError, TimeoutError, OSError):
            return Batch([Observation({'reachable': False}, kind='health')], cursor)
        return Batch([Observation({'reachable': True, 'status': code, 'healthy': 200 <= code < 400}, kind='health')], cursor)


class Clock:
    """Emit one event per wall-clock bucket; use interval_seconds for polling."""
    def __init__(self, options):
        self.period = options.get('period_seconds', 3600)
        if type(self.period) is not int or not 1 <= self.period <= 604800:
            raise ValueError('Invalid clock period')

    def collect(self, *, cursor, now: datetime):
        bucket = int(now.timestamp()) // self.period
        if cursor == bucket:
            return Batch([], bucket)
        return Batch([Observation({'bucket': bucket, 'period_seconds': self.period}, kind='clock.tick')], bucket)
