import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from agent_hub import Conflict, HubStore, encoded


class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state'
        self.store = HubStore(self.path)
        self.a = self.store.create_context('Музыка')['id']
        self.b = self.store.create_context('Домашняя сеть')['id']

    def test_context_and_focus_survive_restart(self):
        self.store.append(self.a,'user','Что сейчас играет?','phone')
        self.store.switch('phone',self.a,0)
        reopened = HubStore(self.path)
        self.assertEqual(reopened.focus('phone')['context_id'],self.a)
        self.assertEqual(reopened.packet(self.a)['entries'][0]['body'],'Что сейчас играет?')
        self.assertEqual(reopened.focus('desktop')['revision'],0)

    def test_delivery_replay_is_idempotent_but_content_change_is_rejected(self):
        entry_id = str(uuid.uuid4())
        first = self.store.append(self.a,'tool','playing','desktop',entry_id)
        self.assertEqual(first,self.store.append(self.a,'tool','playing','desktop',entry_id))
        with self.assertRaises(Conflict):
            self.store.append(self.b,'tool','playing','desktop',entry_id)
        self.assertEqual(len(self.store.packet(self.a)['entries']),1)

    def test_global_and_topic_memory_are_separate_and_versioned(self):
        self.store.put_memory('language','русский','user',0)
        self.store.put_memory('choice','playlist','user',0,self.a)
        self.store.put_memory('choice','router','user',0,self.b)
        facts = self.store.packet(self.a)['memory']
        self.assertEqual({f['body'] for f in facts},{'русский','playlist'})
        with self.assertRaises(Conflict):
            self.store.put_memory('choice','wrong','user',0,self.a)
        deleted = self.store.put_memory('choice','','user',1,self.a,deleted=True)
        self.assertEqual(deleted['revision'],2)
        self.assertEqual(deleted['body'],'')
        with self.assertRaises(Conflict):
            self.store.put_memory('choice','stale','user',1,self.a)
        self.assertEqual(len(self.store.packet(self.a)['memory']),1)
        self.assertEqual(len(self.store.memories(self.a,include_deleted=True)),2)

    def test_notification_does_not_steal_focus_and_follow_is_explicit(self):
        self.store.switch('phone',self.a,0)
        self.store.switch('desktop',self.b,0)
        notification = self.store.notify(self.b,'hermes','Проверка сети завершена')
        self.assertEqual(self.store.focus('phone')['context_id'],self.a)
        self.store.switch('phone',self.a,1)
        with self.assertRaises(Conflict):
            self.store.follow_notification('phone',notification['id'],1)
        result = self.store.follow_notification('phone',notification['id'],2)
        self.assertEqual(result['context_id'],self.b)
        self.assertEqual(self.store.focus('desktop')['revision'],1)

    def test_parallel_memory_writers_cannot_overwrite_each_other(self):
        barrier = threading.Barrier(2)
        results = []
        def write(body):
            try:
                barrier.wait(timeout=5)
                self.store.put_memory('shared',body,'agent',0)
                results.append('saved')
            except Conflict:
                results.append('conflict')
        workers = [threading.Thread(target=write,args=(body,)) for body in ('one','two')]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=5)
        self.assertEqual(sorted(results),['conflict','saved'])
        self.assertEqual(self.store.memories()[0]['revision'],1)

    def test_chat_identity_includes_host_adapter_and_preserves_topic(self):
        first = self.store.register_chat('mac','codex','native-1','Тест','/project',self.a)
        refreshed = self.store.register_chat('mac','codex','native-1','Новое имя','/project')
        self.assertEqual(first['id'],refreshed['id'])
        self.assertEqual(refreshed['context_id'],self.a)
        other = self.store.register_chat('nas','codex','native-1','Тест')
        self.assertNotEqual(first['id'],other['id'])
        with self.assertRaises(Conflict):
            self.store.register_chat('mac','codex','native-1',context=self.b)
        self.assertEqual(len(self.store.chats(self.a)),1)
        self.assertEqual(self.store.packet(self.b)['chats'],[])

    def test_export_budget_uses_utf8_and_reports_omission(self):
        for i in range(110):
            self.store.append(self.a,'user',f'{i} ' + 'я' * 500,'phone')
        self.store.append(self.b,'user','PRIVATE OTHER TOPIC','desktop')
        packet = self.store.packet(self.a,2048)
        self.assertLessEqual(len(encoded(packet).encode()),2048)
        self.assertGreater(packet['omitted']['entries'],0)
        self.assertEqual(packet['omitted']['entries'] + len(packet['entries']),110)
        self.assertNotIn('PRIVATE OTHER TOPIC',encoded(packet))
        self.assertEqual(packet['entries'],sorted(packet['entries'],key=lambda e:e['seq']))
        self.assertEqual(len(self.store.packet(self.a,256000)['entries']),100)

    def test_private_storage_rejects_unsafe_directory_and_symlink(self):
        public = Path(self.tmp.name) / 'public'
        public.mkdir(mode=0o755)
        with self.assertRaises(ValueError): HubStore(public)
        self.assertEqual(public.stat().st_mode & 0o777,0o755)
        alias = Path(self.tmp.name) / 'alias'; alias.symlink_to(self.path)
        with self.assertRaises(ValueError): HubStore(alias)
        self.assertEqual(self.store.path.stat().st_mode & 0o777,0o600)

    def test_console_commands_use_the_same_persistent_store(self):
        program = Path(__file__).with_name('agent_hub.py')
        def invoke(*args):
            result = subprocess.run([sys.executable,str(program),'--state',str(self.path),*args],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        context = invoke('context-create','Поездка')['id']
        invoke('use','console',context,'--revision','0')
        self.assertEqual(invoke('focus','console')['context_id'],context)
        invoke('append',context,'--role','user','--source','console','Найти маршрут')
        self.assertEqual(invoke('export',context)['entries'][0]['body'],'Найти маршрут')


if __name__ == '__main__': unittest.main()
