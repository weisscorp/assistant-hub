"""Durable single-worker queue for a trusted, configured home-agent command (POSIX).
Bridge credentials are excluded from storage. Queued input/results are private user data.
Terminal jobs discard their input text from the live database.
"""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

ACTIVE = ('queued', 'running', 'cancelling')
TERMINAL = ('succeeded', 'failed', 'cancelled', 'interrupted', 'timed_out')


def job_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('invalid job id')
    return value


class QueueFull(Exception):
    pass


class Conflict(Exception):
    pass


class JobStore:
    def __init__(self, argv, directory, timeout=1800, max_pending=16, retention=604800):
        if not math.isfinite(timeout) or timeout <= 0 or not 1 <= max_pending <= 1000 or retention <= 0:
            raise ValueError("Invalid queue limits")
        if any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("Agent argv must contain nonempty strings")
        self.argv = list(argv)
        self.timeout = timeout
        self.max_pending = max_pending
        self.retention = retention
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stat = self.directory.stat()
        if stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
            raise ValueError("Use a private state directory owned by the server user (mode 0700)")
        self.owner = (self.directory / 'queue.lock').open('a')
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            raise RuntimeError('Only one job worker may own this state directory')
        self.database = sqlite3.connect(self.directory / 'jobs.sqlite3', check_same_thread=False)
        os.chmod(self.directory / 'jobs.sqlite3', 0o600)
        self.database.row_factory = sqlite3.Row
        self.database.execute('PRAGMA journal_mode=WAL')
        self.database.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, fingerprint TEXT, text TEXT, state TEXT, reply TEXT, error TEXT, created REAL, updated REAL)')
        self.database.execute("UPDATE jobs SET state='interrupted', text='', error='server_restarted', updated=? WHERE state='running'", (time.time(),))
        self.database.execute("UPDATE jobs SET state='cancelled', text='', updated=? WHERE state='cancelling'", (time.time(),))
        self.database.commit()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = False
        self.process = None
        self.current = None
        self.worker = threading.Thread(target=self._work, name='home-agent-jobs', daemon=True)
        self.worker.start()

    def _prune(self):
        self.database.execute("DELETE FROM jobs WHERE state NOT IN ('queued','running','cancelling') AND updated < ?", (time.time() - self.retention,))
        self.database.commit()

    def get(self, identifier):
        job_id(identifier)
        with self.lock:
            row = self.database.execute('SELECT id,state,reply,error,created,updated FROM jobs WHERE id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def submit(self, identifier, text):
        job_id(identifier)
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 2000:
            raise ValueError('invalid text')
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        with self.lock:
            if self.closed:
                raise RuntimeError('queue closed')
            self._prune()
            previous = self.database.execute('SELECT fingerprint,state FROM jobs WHERE id=?', (identifier,)).fetchone()
            if previous:
                # A cancelled unknown ID is a tombstone against a delayed POST.
                if previous['fingerprint'] and previous['fingerprint'] != fingerprint:
                    raise Conflict('id already belongs to another request')
                return self.get(identifier)
            pending = self.database.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','cancelling')").fetchone()[0]
            total = self.database.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            if pending >= self.max_pending or total >= 1000:
                raise QueueFull()
            now = time.time()
            self.database.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)', (identifier, fingerprint, text, 'queued', '', '', now, now))
            self.database.commit()
            result = self.get(identifier)
        self.wake.set()
        return result

    def cancel(self, identifier):
        job_id(identifier)
        with self.lock:
            if self.closed:
                raise RuntimeError('queue closed')
            self._prune()
            row = self.database.execute('SELECT state FROM jobs WHERE id=?', (identifier,)).fetchone()
            now = time.time()
            if row is None:
                if self.database.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] >= 1000:
                    raise QueueFull()
                self.database.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)', (identifier, '', '', 'cancelled', '', '', now, now))
            elif row['state'] in ACTIVE:
                state = 'cancelled' if row['state'] == 'queued' else 'cancelling'
                self.database.execute("UPDATE jobs SET state=?,text='',updated=? WHERE id=?", (state, now, identifier))
                if self.current == identifier and self.process is not None:
                    try: self.process.send_signal(signal.SIGTERM)
                    except ProcessLookupError: pass
            self.database.commit()
            return self.get(identifier)

    def _terminal(self, identifier, state, reply='', error=''):
        self.database.execute("UPDATE jobs SET state=?,text='',reply=?,error=?,updated=? WHERE id=? AND state IN ('running','cancelling')", (state, reply, error, time.time(), identifier))
        self.database.commit()

    def _work(self):
        while True:
            with self.lock:
                if self.closed:
                    return
                row = self.database.execute("SELECT id,text FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone() if self.argv else None
                if row:
                    self.database.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), row['id']))
                    self.database.commit()
                    self.current = row['id']
            if not row:
                self.wake.wait(.5); self.wake.clear()
                continue
            self._run(row['id'], row['text'])
            with self.lock:
                self.process = None; self.current = None

    def _run(self, identifier, text):
        read_fd, write_fd = os.pipe()
        output = bytearray()
        excessive = threading.Event()
        readers = []
        process = None
        failed = False
        try:
            with self.lock:
                if self.closed or self.get(identifier)['state'] != 'running':
                    self._terminal(identifier, 'interrupted' if self.closed else 'cancelled', error='server_stopped' if self.closed else '')
                    return
                environment = os.environ.copy()
                for name in ("ASSISTANT_TOKEN", "TLS_CERT", "TLS_KEY", "KEYSTORE_PASSWORD", "KEY_PASSWORD"):
                    environment.pop(name, None)
                environment['HELMET_AGENT_ARGV'] = json.dumps(self.argv)
                environment['HELMET_JOB_ID'] = identifier
                process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('job_runner.py')), '--lifeline-fd', str(read_fd), '--timeout', str(self.timeout)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(read_fd,), env=environment, shell=False)
                self.process = process
            os.close(read_fd); read_fd = None

            def drain(stream, retain):
                size = 0
                with stream:
                    while chunk := stream.read(4096):
                        size += len(chunk)
                        if retain and len(output) < 32768:
                            output.extend(chunk[:32768 - len(output)])
                        if size > 8 * 1024 * 1024:
                            excessive.set()
                            try: process.terminate()
                            except ProcessLookupError: pass
            for stream, retain in ((process.stdout, True), (process.stderr, False)):
                reader = threading.Thread(target=drain, args=(stream, retain), daemon=True)
                reader.start(); readers.append(reader)
            process.stdin.write(text.encode()); process.stdin.close()
            code = process.wait(timeout=self.timeout + 10)
            for reader in readers:
                reader.join(timeout=3)
            with self.lock:
                if self.get(identifier)['state'] == 'cancelling' and code in (130, -signal.SIGTERM):
                    self._terminal(identifier, 'cancelled')
                elif self.closed:
                    self._terminal(identifier, 'interrupted', error='server_stopped')
                elif excessive.is_set():
                    self._terminal(identifier, 'failed', error='output_limit')
                elif code == 124:
                    self._terminal(identifier, 'timed_out', error='agent_timeout')
                elif code != 0 or not output.decode('utf-8', errors='replace').strip():
                    self._terminal(identifier, 'failed', error='agent_failed')
                else:
                    self._terminal(identifier, 'succeeded', reply=output.decode('utf-8', errors='replace').strip()[:1500])
        except (OSError, subprocess.SubprocessError):
            failed = True
        finally:
            os.close(write_fd)  # Supervisor also observes EOF after abrupt parent process death.
            if read_fd is not None:
                os.close(read_fd)
            if process is not None and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=4)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            for reader in readers:
                reader.join(timeout=3)

            if failed:
                with self.lock:
                    stopped = process is None or process.returncode in (130, -signal.SIGTERM)
                    state = 'cancelled' if self.get(identifier)['state'] == 'cancelling' and stopped else 'interrupted' if self.closed else 'failed'
                    self._terminal(identifier, state, error='' if state == 'cancelled' else 'server_stopped' if self.closed else 'agent_failed')

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.process is not None:
                try: self.process.terminate()
                except ProcessLookupError: pass
        self.wake.set()
        self.worker.join(timeout=15)
        if self.worker.is_alive():
            raise RuntimeError('Job supervisor did not stop')
        with self.lock:
            self.database.close()
            fcntl.flock(self.owner, fcntl.LOCK_UN)
            self.owner.close()
