"""Owner-managed Hermes endpoints. Secret files never enter browser responses.

Switches affect new turns only. Profiles and revisions survive restarts without
editing the launch configuration or replacing the previous working provider.
"""
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from agent_hub import Conflict
from hub_hermes_api import turn


def vpn_endpoint(value):
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError('Укажите VPN-адрес Hermes с окончанием /v1')
    url = urllib.parse.urlsplit(value)
    if (url.scheme not in {'http', 'https'} or not url.hostname or url.username is not None or
            url.password is not None or url.query or url.fragment or url.path.rstrip('/') != '/v1'):
        raise ValueError('Нужен адрес http(s)://сервер:порт/v1 без ключа и параметров')
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError('Некорректный порт')
    try:
        address = ipaddress.ip_address(url.hostname)
        if address not in ipaddress.ip_network('100.64.0.0/10') and not address.is_loopback:
            raise ValueError('Разрешены только VPN-адреса Tailscale или loopback')
    except ValueError as error:
        if not url.hostname.endswith('.ts.net') or url.scheme != 'https':
            raise ValueError('Используйте HTTPS-имя Tailscale (*.ts.net) или VPN-IP 100.64.0.0/10') from error
    return value.rstrip('/')


def check_dns(value):
    url = urllib.parse.urlsplit(vpn_endpoint(value))
    try:
        addresses = socket.getaddrinfo(url.hostname, url.port or (443 if url.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError('VPN-имя сервера недоступно. Проверьте Tailscale и адрес.') from None
    if not addresses or any(not (ipaddress.ip_address(row[4][0]).is_loopback or
                            ipaddress.ip_address(row[4][0]) in ipaddress.ip_network('100.64.0.0/10')) for row in addresses):
        raise ValueError('Имя сервера должно разрешаться только в VPN-адрес Tailscale')


def read_private(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 64000:
        raise ValueError('Файл настроек должен принадлежать Hub и иметь права 0600')
    return path.read_text()


def atomic_private(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.connection-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class HermesConnections:
    def __init__(self, directory, legacy=None):
        self.directory = Path(directory) / 'hermes-connections'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / 'profiles.json'
        self.lock = threading.RLock()
        self.legacy = legacy
        if self.path.exists():
            self.data = json.loads(read_private(self.path))
        else:
            self.data = {'revision': 0, 'active': None, 'profiles': []}
            # Preserve the installed connection as an editable profile when it is
            # the known HTTP bridge, never execute or import an arbitrary argv.
            argv = getattr(legacy, 'argv', [])
            if len(argv) >= 4 and Path(argv[1]).name == 'hub_hermes_api.py' and '--config' in argv:
                try:
                    config = json.loads(read_private(argv[argv.index('--config') + 1]))
                    secret = read_private(config['key_file']).strip()
                    url = vpn_endpoint(config['url'])
                    profile = {'id': str(uuid.uuid4()), 'name': 'Текущее подключение Hermes',
                               'url': url, 'model': config.get('model', 'hermes-agent'), 'updated_at': time.time()}
                    atomic_private(self.directory / (profile['id'] + '.key'), secret)
                    self.data['profiles'].append(profile)
                    # Keep the original command adapter active until an explicit switch.
                except (KeyError, ValueError, OSError):
                    pass
            self._persist()

    def _persist(self):
        atomic_private(self.path, json.dumps(self.data, ensure_ascii=False))

    def snapshot(self):
        with self.lock:
            value = json.loads(json.dumps({**self.data, 'legacy_available': self.legacy is not None}))
            for profile in value['profiles']: profile.pop('key_id', None)
            return value

    def _secret(self, profile):
        key_id = profile.get('key_id', profile['id'])
        if str(uuid.UUID(key_id)) != key_id: raise ValueError('Invalid credential reference')
        return read_private(self.directory / (key_id + '.key')).strip()

    def _profile(self, profile_id):
        return next((p for p in self.data['profiles'] if p['id'] == profile_id), None)

    def _revision(self, value):
        if type(value) is not int or value != self.data['revision']:
            raise Conflict('Настройки уже изменились. Обновите список подключений.')

    def _candidate(self, body):
        profile_id = body.get('id')
        current = self._profile(profile_id) if profile_id else None
        if profile_id and not current:
            raise ValueError('Подключение не найдено')
        name = body.get('name', '')
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('Название: от 1 до 100 символов')
        name = name.strip()
        url = vpn_endpoint(body.get('url'))
        model = body.get('model', 'hermes-agent')
        if not isinstance(model, str) or not model.strip() or len(model) > 128:
            raise ValueError('Укажите имя модели, которое принимает сервер')
        secret = body.get('token')
        if not secret and current:
            # Never silently send a saved credential to a newly entered server.
            if url != current['url']:
                raise ValueError('При смене адреса введите отдельный ключ нового сервера')
            secret = self._secret(current)
        if not isinstance(secret, str) or not 16 <= len(secret) <= 8192 or any(c.isspace() for c in secret):
            raise ValueError('Введите ключ сервера Hermes (без пробелов)')
        profile = {'id': profile_id or str(uuid.uuid4()), 'name': name, 'url': url, 'model': model.strip(), 'updated_at': time.time()}
        return profile, secret

    def probe(self, profile, secret):
        check_dns(profile['url'])
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        req = urllib.request.Request(profile['url'] + '/models', headers={'Authorization': 'Bearer ' + secret})
        try:
            with opener.open(req, timeout=8) as response:
                data = response.read(65537)
            if len(data) > 65536: raise ValueError('Слишком большой ответ сервера')
            models = json.loads(data).get('data', [])
            if not any(m.get('id') == profile['model'] for m in models if isinstance(m, dict)):
                raise ValueError('Сервер доступен, но выбранная модель отсутствует в /v1/models')
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}: raise ValueError('Сервер не принял ключ доступа') from None
            raise ValueError('Сервер вернул HTTP ' + str(error.code)) from None
        except (OSError, urllib.error.URLError):
            raise ValueError('Нет связи с Hermes. Проверьте VPN, адрес и работу сервера.') from None
        return {'ready': True, 'detail': 'Связь и ключ проверены. Запрос к модели не запускался.', 'checked_at': time.time()}

    def save(self, body, save=True):
        with self.lock:
            self._revision(body.get('revision'))
            profile, secret = self._candidate(body)
            if not profile.get('id') in {p['id'] for p in self.data['profiles']} and len(self.data['profiles']) >= 20:
                raise ValueError('Достигнут лимит: 20 подключений')
            checked = self.probe(profile, secret)
            if save:
                # Immutable credential generations keep the old connection intact
                # even when the subsequent profile-file write fails.
                key_id = str(uuid.uuid4())
                atomic_private(self.directory / (key_id + '.key'), secret)
                before = self.data
                self.data = {**before, 'profiles': [p for p in before['profiles'] if p['id'] != profile['id']] +
                             [{**profile, **checked, 'key_id': key_id}], 'revision': before['revision'] + 1}
                try: self._persist()
                except BaseException:
                    self.data = before
                    raise
            return {'connection': profile, 'check': checked, 'settings': self.snapshot()}

    def activate(self, body):
        with self.lock:
            self._revision(body.get('revision'))
            profile_id = body.get('id')
            if profile_id is None:
                if self.legacy is None: raise ValueError('Исходный сервер не настроен')
            else:
                profile = self._profile(profile_id)
                if not profile: raise ValueError('Подключение не найдено')
                self.probe(profile, self._secret(profile))
            before = self.data
            self.data = {**before, 'active': profile_id, 'revision': before['revision'] + 1}
            try: self._persist()
            except BaseException:
                self.data = before
                raise
            return self.snapshot()

    def status(self):
        with self.lock:
            profile = self._profile(self.data['active'])
            if profile:
                return {'ready': True, 'detail': profile['name'] + ' · ' + profile['model']}
            if self.legacy:
                return self.legacy.status()
            return {'ready': False, 'detail': 'Добавьте сервер Hermes в разделе «Подключения»'}

    def run(self, row, coordinator, stop, progress):
        with self.lock:
            profile = self._profile(self.data['active'])
            if profile:
                # Snapshot the credential for this turn; edits apply to the next.
                profile = dict(profile)
                secret = self._secret(profile)
        if profile is None:
            if self.legacy: return self.legacy.run(row, coordinator, stop, progress)
            raise ValueError('Сервер Hermes не настроен')
        if stop.is_set(): raise InterruptedError('cancelled')
        check_dns(profile['url'])
        from hub_providers import INSTRUCTIONS
        reply = turn({'url': profile['url'], 'model': profile['model']},
                     {'task': row, 'context': coordinator.context_packet(row), 'instructions': INSTRUCTIONS}, secret=secret)
        if stop.is_set(): raise InterruptedError('cancelled')
        return reply
