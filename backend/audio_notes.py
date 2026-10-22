"""Separate durable audio notes: private WAV originals, transcripts and stable IDs."""
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import wave
import io
from pathlib import Path
from home_link import identifier, integer, text, encoded

MAX_AUDIO = 20 * 1024 * 1024

class AudioNotes:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs = self.directory / 'audio'
        self.blobs.mkdir(exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS audio_notes(id TEXT PRIMARY KEY, device TEXT NOT NULL, created INTEGER NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS audio_notes_created ON audio_notes(created,id)')
        os.chmod(self.directory / 'notes.sqlite', 0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.directory / 'notes.sqlite', timeout=30)
        db.create_function('casefold',1,lambda value:value.casefold())
        try:
            with db: yield db
        finally: db.close()

    def blob(self, sha, data=None):
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('invalid audio digest')
        path = self.blobs / (sha + '.wav')
        if data is None:
            if not path.is_file(): raise KeyError('unknown audio')
            return path.read_bytes()
        if not 44 < len(data) <= MAX_AUDIO or hashlib.sha256(data).hexdigest() != sha:
            raise ValueError('invalid audio bytes/digest')
        try:
            with wave.open(io.BytesIO(data)) as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1,2,16000,'NONE'):
                    raise ValueError('expected mono PCM16 16kHz WAV')
                frames = audio.getnframes()
                if not 0 < frames <= 16000 * 600 or len(audio.readframes(frames)) != frames * 2:
                    raise ValueError('invalid recording length')
        except (wave.Error, EOFError) as error:
            raise ValueError('invalid WAV') from error
        fd, temporary = tempfile.mkstemp(dir=self.blobs)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return {'sha256':sha}

    def put(self, device, note):
        identifier(device)
        if not isinstance(note, dict) or set(note) != {'id','title','text','createdAt','durationMs','audioSha256','status','revision'}:
            raise ValueError('invalid audio note')
        identifier(note['id']); text(note['title'],100); text(note['text'],16000,True)
        integer(note['createdAt'],1); integer(note['durationMs'],1,600000); integer(note['revision'],1)
        if note['status'] not in ('pending','ready','error'): raise ValueError('invalid transcription status')
        audio = self.blob(note['audioSha256'])
        with wave.open(io.BytesIO(audio)) as stream:
            if note['durationMs'] != stream.getnframes() * 1000 // 16000:
                raise ValueError('duration differs from recording')
        value = {**note, 'device':device}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT device,data FROM audio_notes WHERE id=?',(note['id'],)).fetchone()
            if old:
                previous = json.loads(old[1])
                if old[0] != device: raise ValueError('note belongs to another device')
                if any(previous[k] != value[k] for k in ('createdAt','durationMs','audioSha256')):
                    raise ValueError('recording is immutable')
                if previous['revision'] > note['revision']: return previous
                if previous['revision'] == note['revision'] and previous != value:
                    raise ValueError('note revision conflict')
            db.execute('INSERT OR REPLACE INTO audio_notes VALUES(?,?,?,?)',
                       (note['id'],device,note['createdAt'],encoded(value).decode()))
        return value

    def read(self, query='', note_id='', offset=0, limit=10):
        text(query,200,True); integer(offset,0); integer(limit,1,50)
        if note_id: identifier(note_id)
        # Bound pages and expose a cursor; callers can access every note without a giant model prompt.
        with self.connect() as db:
            if note_id:
                row = db.execute('SELECT data FROM audio_notes WHERE id=?',(note_id,)).fetchone()
                if not row: raise KeyError('unknown note')
                return json.loads(row[0])
            pattern = '%' + query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
            rows = db.execute("SELECT data FROM audio_notes WHERE casefold(data) LIKE casefold(?) ESCAPE '\\' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",(pattern,limit+1,offset)).fetchall()
        return {'items':[json.loads(r[0]) for r in rows[:limit]], 'next':offset+limit if len(rows)>limit else None}
