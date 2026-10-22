from contextlib import closing
import hashlib
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from hub_updates import Updates, Supervisor, RUNTIME, atomic
import test_hub_auth as auth_fixture


def bundle(release='fixture-1', edits=None, extra=None):
    files={'backend/hub_container.py':b'pass\n','backend/hub_server.py':b'pass\n','backend/hub_ui/index.html':b'fixture'}
    files.update(edits or {})
    manifest={'version':release,'runtime':RUNTIME,'files':{k:hashlib.sha256(v).hexdigest() for k,v in files.items()}}
    files['release.json']=json.dumps(manifest).encode()
    data=io.BytesIO()
    with tarfile.open(fileobj=data,mode='w:gz') as archive:
        for name,raw in files.items():
            item=tarfile.TarInfo(name);item.size=len(raw);archive.addfile(item,io.BytesIO(raw))
        if extra:archive.addfile(extra)
    return data.getvalue()


class UpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.updates=Updates(self.root/'updates')
        atomic(self.updates.root/'control.json',{'phase':'running','active':'initial','previous':None})

    def tearDown(self):self.temp.cleanup()

    def stage(self,data):return self.updates.stage(data,hashlib.sha256(data).hexdigest())

    def test_stage_does_not_execute_or_activate(self):
        data=bundle(edits={'backend/hub_server.py':b'raise RuntimeError("do not execute at upload")'})
        status=self.stage(data)
        self.assertEqual(status['active'],'initial')
        self.assertEqual(status['releases'][0]['version'],'fixture-1')
        self.stage(data)  # Same package is idempotent.
        with self.assertRaises(ValueError):self.stage(bundle(edits={'backend/hub_server.py':b'print("changed")'}))

    def test_wrong_hash_invalid_archive_and_syntax_are_rejected(self):
        with self.assertRaises(ValueError):self.updates.stage(bundle(),'0'*64)
        for raw in [b'not gzip',bundle(edits={'backend/hub_server.py':b'def : invalid'})]:
            with self.assertRaises(ValueError):self.stage(raw)
        self.assertEqual(self.updates.status()['releases'],[])

    def test_symlinks_traversal_and_non_code_paths_are_rejected(self):
        for name in ['../../secrets','/absolute','backend/../oops','backend//alias.py','state/config.json','backend/binary.sh']:
            item=tarfile.TarInfo(name);item.size=0
            with self.subTest(name=name),self.assertRaises(ValueError):self.stage(bundle(extra=item))
        item=tarfile.TarInfo('backend/link.py');item.type=tarfile.SYMTYPE;item.linkname='/etc/passwd'
        with self.assertRaises(ValueError):self.stage(bundle(extra=item))

    def test_request_requires_staged_release_and_serializes_switches(self):
        with self.assertRaises(ValueError):self.updates.request('missing')
        self.stage(bundle())
        state=self.updates.request('fixture-1')
        self.assertEqual(state['phase'],'queued');self.assertTrue(self.updates.barrier())
        with self.assertRaises(ValueError):self.updates.request('fixture-1')
        again=Updates(self.updates.root);self.assertTrue(again.barrier())

    def test_failed_candidate_restores_database_and_starts_old_code(self):
        data=self.root/'data';state=data/'state';state.mkdir(parents=True)
        path=state/'agent-hub.db'
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE fixture(value)');db.execute('INSERT INTO fixture VALUES("before")');db.commit()
        class Child:
            def poll(self):return 1
        class Controlled(Supervisor):
            def stop(self):pass
            def start(self,release):
                self.started.append(release);self.child=Child()
                if release=='fixture-1':
                    with closing(sqlite3.connect(path)) as db:
                        db.execute('UPDATE fixture SET value="broken migration"');db.commit()
        supervisor=Controlled(self.updates,'initial');supervisor.started=[]
        self.stage(bundle());self.updates.request('fixture-1')
        with patch.dict(os.environ,{'HUB_DATA_DIR':str(data)}):supervisor.apply(self.updates.control())
        self.assertEqual(supervisor.started,['fixture-1','initial'])
        self.assertEqual(self.updates.status()['phase'],'rolled-back')
        with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('SELECT value FROM fixture').fetchone()[0],'before')
        self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_success_commits_after_health_probe(self):
        class Child:
            def poll(self):return None
        class Controlled(Supervisor):
            def stop(self):pass
            def start(self,release):self.child=Child()
            def healthy(self,release):return True
            def backup(self,request):return '/synthetic/backup'
        self.stage(bundle());self.updates.request('fixture-1')
        Controlled(self.updates,'initial').apply(self.updates.control())
        status=self.updates.status();self.assertEqual(status['active'],'fixture-1');self.assertEqual(status['previous'],'initial')
        self.assertFalse(self.updates.barrier())

    def test_failed_restore_never_starts_hub_on_supervisor_restart(self):
        atomic(self.updates.root/'control.json',{'phase':'recovery-needed','active':'initial','previous':None})
        supervisor=Supervisor(self.updates,'initial')
        with patch('hub_updates.signal.signal'),patch.object(supervisor,'start') as start:
            with self.assertRaises(RuntimeError):supervisor.run()
            start.assert_not_called()


class UpdateApiTests(unittest.TestCase):
    setUp=auth_fixture.PublicApiTests.setUp
    tearDown=auth_fixture.PublicApiTests.tearDown
    request=auth_fixture.PublicApiTests.request
    setup_account=auth_fixture.PasswordApiTests.setup_account
    origin=auth_fixture.PasswordApiTests.origin

    def test_app_and_old_owner_cannot_install_publisher_cannot_activate(self):
        cookie=self.setup_account()
        self.app.updates=Updates(Path(self.tmp.name)/'updates')
        atomic(self.app.updates.root/'control.json',{'phase':'running','active':'initial','previous':None})
        self.assertEqual(self.request('/api/releases',headers=cookie)[0],200)
        for scope in ['owner','app','mcp','webhook','deploy']:
            _,key=self.app.auth.issue('fixture '+scope,scope)
            self.assertEqual(self.request('/api/releases/activate',key,{'version':'fixture-1'})[0],403)
            self.assertEqual(self.request('/api/connections',key)[0],403)
        _,publisher=self.app.auth.issue('publisher','deploy')
        self.assertEqual(self.request('/api/state',publisher)[0],403)

    def test_busy_hub_rejects_switch(self):
        cookie=self.setup_account()
        self.app.updates=Updates(Path(self.tmp.name)/'updates')
        atomic(self.app.updates.root/'control.json',{'phase':'running','active':'initial','previous':None})
        raw=bundle();self.app.updates.stage(raw,hashlib.sha256(raw).hexdigest())
        self.app.coordinator.active['synthetic-task']=object()
        self.assertEqual(self.request('/api/releases/activate',body={'version':'fixture-1'},headers=cookie)[0],409)
        self.assertEqual(self.app.updates.control()['phase'],'running')

    def test_publisher_binary_upload_stages_without_activation(self):
        self.app.updates=Updates(Path(self.tmp.name)/'updates')
        atomic(self.app.updates.root/'control.json',{'phase':'running','active':'initial','previous':None})
        _,key=self.app.auth.issue('fixture publisher','deploy')
        raw=bundle()
        connection=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        headers={'Host':'hub.example.com','X-Forwarded-Proto':'https','X-Forwarded-For':'203.0.113.5',
                 'Authorization':'Bearer '+key,'Content-Type':'application/gzip','X-Release-SHA256':hashlib.sha256(raw).hexdigest()}
        connection.request('POST','/api/releases/stage',raw,headers)
        response=connection.getresponse();value=json.loads(response.read());connection.close()
        self.assertEqual(response.status,201);self.assertEqual(value['active'],'initial')
        self.assertEqual(value['releases'][0]['version'],'fixture-1')
        self.assertEqual(self.request('/api/releases/activate',key,{'version':'fixture-1'})[0],403)


if __name__=='__main__':unittest.main()
