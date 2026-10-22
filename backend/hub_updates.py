"""Versioned, owner-requested Hub source updates inside the existing container.

No Docker socket, host shell, registry password or NAS admin token. The pinned
supervisor is installed once; releases may update only the backend payload.
"""
import argparse
import ast
from contextlib import closing, contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid

MAX_ARCHIVE = 4 * 1024 * 1024
RUNTIME = 'hub-python313-stdlib-v1'


def atomic(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.update-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}', value):
        raise ValueError('Некорректное имя релиза')
    return value


class Updates:
    def __init__(self, root=None):
        self.root = Path(root).resolve() if root else None
        if self.root:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            (self.root / 'releases').mkdir(mode=0o700, exist_ok=True)

    @contextmanager
    def locked(self):
        if not self.root: raise ValueError('Служба обновлений ещё не установлена')
        fd = os.open(self.root / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def control(self):
        path = self.root / 'control.json'
        return json.loads(path.read_text()) if path.exists() else {'phase': 'unconfigured'}

    def status(self):
        if not self.root: return {'enabled': False, 'detail': 'Нужна однократная установка службы обновлений'}
        with self.locked():
            control = self.control()
            releases = []
            for path in sorted((self.root / 'releases').glob('*/release.json')):
                meta = json.loads(path.read_text())
                releases.append({k: meta.get(k) for k in ('version', 'runtime', 'sha256', 'uploaded_at')})
            return {'enabled': control['phase'] != 'unconfigured', 'phase': control['phase'],
                    'active': control.get('active'), 'previous': control.get('previous'),
                    'error': control.get('error'), 'releases': releases}

    def stage(self, raw, digest):
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_ARCHIVE:
            raise ValueError('Пакет должен быть не больше 4 МиБ')
        if not isinstance(digest, str) or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Контрольная сумма пакета не совпала')
        with self.locked():
            temp = Path(tempfile.mkdtemp(prefix='.stage-', dir=self.root))
            try:
                with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
                    names, total = set(), 0
                    for count, member in enumerate(archive, 1):
                        total += member.size
                        if count > 1000 or total > 16 * 1024 * 1024:
                            raise ValueError('Неподдерживаемый размер содержимого пакета')
                        path = PurePosixPath(member.name)
                        if (not member.isfile() or path.is_absolute() or '..' in path.parts or path.as_posix() != member.name or
                                not re.fullmatch(r'[A-Za-z0-9_./-]+', member.name) or member.name in names or
                                member.size > 1024 * 1024 or
                                (member.name != 'release.json' and (path.parts[0] != 'backend' or path.suffix not in {'.py', '.js', '.html', '.css', '.json'}))):
                            raise ValueError('Пакет содержит недопустимый путь или тип файла')
                        names.add(member.name)
                        target = temp.joinpath(*path.parts)
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        target.write_bytes(archive.extractfile(member).read())
                        target.chmod(0o600)
                meta = json.loads((temp / 'release.json').read_text())
                release = version(meta['version'])
                if meta.get('runtime') != RUNTIME:
                    raise ValueError('Релиз требует другую версию контейнера или зависимостей')
                files = meta.get('files')
                if not isinstance(files, dict) or set(files) != names - {'release.json'}:
                    raise ValueError('Список файлов релиза не совпал с архивом')
                for name, expected in files.items():
                    data = (temp / name).read_bytes()
                    if hashlib.sha256(data).hexdigest() != expected: raise ValueError('Повреждён файл релиза')
                    if name.endswith('.py'): ast.parse(data, filename=name)
                required = {'backend/hub_container.py', 'backend/hub_server.py', 'backend/hub_ui/index.html'}
                if not required <= set(files): raise ValueError('Пакет не содержит весь Hub')
                target = self.root / 'releases' / release
                if target.exists():
                    if json.loads((target / 'release.json').read_text()).get('sha256') != digest:
                        raise ValueError('Это имя релиза уже занято другим пакетом')
                else:
                    meta.update(sha256=digest, uploaded_at=time.time())
                    atomic(temp / 'release.json', meta)
                    temp.rename(target)
            except (tarfile.TarError, SyntaxError, UnicodeError, json.JSONDecodeError, KeyError, EOFError) as error:
                raise ValueError('Некорректный или несовместимый пакет релиза') from None
            finally:
                if temp.exists(): shutil.rmtree(temp)
        return self.status()

    def request(self, target):
        with self.locked():
            control = self.control()
            if control.get('phase') not in {'running', 'rolled-back'}:
                raise ValueError('Обновление уже выполняется или служба не настроена')
            target = version(target)
            if target == control.get('active'): raise ValueError('Этот релиз уже используется')
            if not (self.root / 'releases' / target / 'backend/hub_container.py').is_file():
                raise ValueError('Релиз не загружен')
            control.update(phase='queued', target=target, request_id=str(uuid.uuid4()), error=None)
            atomic(self.root / 'control.json', control)
        return self.status()

    def barrier(self):
        return bool(self.root and self.control().get('phase') in {'queued', 'trial'})


class Supervisor:
    def __init__(self, updates, initial):
        self.updates = updates
        self.initial = version(initial)
        self.child = None
        self.stopping = False

    def write(self, control):
        with self.updates.locked(): atomic(self.updates.root / 'control.json', control)

    def start(self, release):
        backend = self.updates.root / 'releases' / version(release) / 'backend'
        env = {**os.environ, 'HUB_UPDATE_DIR': str(self.updates.root), 'HUB_RELEASE_ID': release}
        self.child = subprocess.Popen([sys.executable, str(backend / 'hub_container.py'), 'serve'], cwd=backend, env=env)

    def stop(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try: self.child.wait(timeout=30)
            except subprocess.TimeoutExpired: self.child.kill(); self.child.wait()

    def healthy(self, release):
        try:
            config = json.loads((Path(os.environ.get('HUB_RUNTIME_DIR', '/tmp/assistant-hub')) / 'config.json').read_text())
            host = config['host']; host = '['+host+']' if ':' in host else host
            key = Path(config['token_file']).read_text().strip()
            request = urllib.request.Request(f'http://{host}:{config["port"]}/health', headers={'Authorization': 'Bearer '+key})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
                value = json.loads(response.read(4096))
            return value.get('status') == 'ok' and value.get('release_id') == release
        except Exception:
            return False

    def backup(self, request_id):
        state = Path(os.environ.get('HUB_DATA_DIR', '/data')) / 'state'
        backup = self.updates.root / 'backups' / request_id
        backup.mkdir(mode=0o700, parents=True)
        for source in state.rglob('*'):
            if source.is_symlink(): continue
            if source.is_file() and source.suffix in {'.db', '.sqlite'}:
                target = backup / source.relative_to(state)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with closing(sqlite3.connect(source)) as db, closing(sqlite3.connect(target)) as dest: db.backup(dest)
                target.chmod(0o600)
        return str(backup)

    def restore(self, backup):
        source = Path(backup).resolve()
        if not source.is_relative_to(self.updates.root / 'backups'): raise ValueError('Invalid backup path')
        state = Path(os.environ.get('HUB_DATA_DIR', '/data')) / 'state'
        for path in source.rglob('*'):
            if path.is_file():
                target = state / path.relative_to(source)
                for suffix in ('-wal', '-shm'):
                    Path(str(target)+suffix).unlink(missing_ok=True)
                shutil.copy2(path, target)
                target.chmod(0o600)

    def apply(self, control):
        before, target = control['active'], control['target']
        self.stop()
        backup = None
        try:
            backup = self.backup(control['request_id'])
            trial = {**control, 'phase': 'trial', 'backup': backup, 'previous': before}
            self.write(trial)
            self.start(target)
            deadline = time.monotonic()+45
            while not self.stopping and time.monotonic() < deadline and self.child.poll() is None:
                if self.healthy(target):
                    self.write({**trial, 'phase': 'running', 'active': target, 'target': None, 'error': None})
                    return
                time.sleep(1)
            self.stop()
        except Exception:
            self.stop()
        if backup:
            try: self.restore(backup)
            except Exception:
                self.write({**control, 'phase':'recovery-needed', 'backup':backup,
                            'error':'Не удалось вернуть резервную копию. Сервер остановлен для восстановления.'})
                self.stopping = True
                return
        self.write({**control, 'phase': 'rolled-back', 'active': before, 'target': None,
                    'error': 'Новая версия не запустилась. Возвращён предыдущий релиз; проверьте журналы.'})
        if not self.stopping: self.start(before)

    def run(self):
        def end(*_): self.stopping = True
        signal.signal(signal.SIGTERM, end); signal.signal(signal.SIGINT, end)
        control = self.updates.control()
        if control['phase'] == 'recovery-needed':
            raise RuntimeError('Database recovery must finish before starting Hub')
        if control['phase'] == 'unconfigured':
            control = {'phase': 'running', 'active': self.initial, 'previous': None}
            self.write(control)
        if control['phase'] == 'trial':
            self.restore(control['backup'])
            control.update(phase='rolled-back', active=control['previous'], target=None,
                           error='Запуск релиза был прерван. Возвращена предыдущая версия.')
            self.write(control)
        self.start(control['active'])
        try:
            while not self.stopping:
                control = self.updates.control()
                if control['phase'] == 'queued': self.apply(control)
                elif self.child.poll() is not None:
                    time.sleep(3)
                    if not self.stopping: self.start(control['active'])
                time.sleep(1)
        finally: self.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['supervise'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--initial', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    Supervisor(Updates(args.root), args.initial).run()


if __name__ == '__main__': main()
