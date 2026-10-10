"""Indexed recall, migration, scope isolation and real SQLite query plans."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from agent_hub import HubStore, encoded, Conflict
from hub_runtime import RuntimeStore, Coordinator
from hub_providers import messages, CommandProvider, OrchestratorProvider
from knowledge_search import query_terms, match_expression, MAX_FACTS, MEMORY_BYTES


class KnowledgeSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RuntimeStore(self.tmp.name+'/state')
        self.context = self.store.create_context('Тема')['id']
        self.other = self.store.create_context('Другая тема')['id']

    def tearDown(self): self.tmp.cleanup()

    def row(self, prompt):
        return self.store.create_task(self.context,'test','test',prompt,str(uuid.uuid4()))[0]

    def test_source_language_alias_and_russian_inflection(self):
        self.store.put_memory('verification_code','синтетика-73','test',0,
                              search_text='Мой проверочный код — синтетика-73.')
        result = self.store.search_memory('Какой мой проверочный код?',self.context)
        self.assertEqual([r['key'] for r in result],['verification_code'])
        self.store.put_memory('audio','Предпочитаю беспроводную гарнитуру','test',0)
        self.assertEqual(self.store.search_memory('О гарнитурах',self.context)[0]['key'],'audio')
        self.store.put_memory('child','Ребёнок учится в школе','test',0)
        self.assertEqual(self.store.search_memory('ребенок',self.context)[0]['key'],'child')

    def test_scope_filters_are_inside_the_fts_query(self):
        self.store.put_memory('shared','Навигатор общий','test',0)
        self.store.put_memory('local','Навигатор этой темы','test',0,self.context)
        self.store.put_memory('hidden','Навигатор PRIVATE OTHER TOPIC','test',0,self.other)
        packet = self.store.packet(self.context,query='Какой навигатор?')
        self.assertEqual({r['key'] for r in packet['memory']},{'shared','local'})
        self.assertNotIn('PRIVATE OTHER TOPIC',encoded(packet))
        self.assertEqual([r['key'] for r in self.store.search_memory('навигатор')],['shared'])

    def test_corrections_deletion_restore_and_conflicts_update_index(self):
        self.store.put_memory('device','Беспроводная гарнитура','test',0,search_text='гарнитура')
        with self.assertRaises(Conflict):
            self.store.put_memory('device','планшет','test',0)
        self.store.put_memory('device','Красный планшет','test',1)
        self.assertEqual(self.store.search_memory('гарнитура'),[])
        self.assertEqual(self.store.search_memory('планшет')[0]['revision'],2)
        self.store.put_memory('device','','test',2,deleted=True)
        self.assertEqual(self.store.search_memory('планшет'),[])
        reopened = RuntimeStore(self.store.directory)
        self.assertEqual(reopened.search_memory('планшет'),[])
        reopened.put_memory('device','Синий планшет','test',3)
        self.assertEqual(reopened.search_memory('планшет')[0]['revision'],4)
        with reopened.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM memory_fts').fetchone()[0],1)

    def test_literal_query_operators_and_no_match_never_fallback(self):
        self.store.put_memory('note','Аэропорт и навигатор','test',0)
        for query in ('', 'а и как это мой', 'метеорит', '" OR * NOT : ()', 'NEAR(*, -1)'):
            self.assertEqual(self.store.packet(self.context,query=query)['memory'],[],query)
        self.assertEqual(len(self.store.search_memory('"аэропорт" OR (навигатор*)',self.context)),1)
        self.assertLessEqual(len(query_terms(' '.join('слово'+str(i) for i in range(200)))),12)

    def test_packets_bound_facts_bytes_and_excerpt_actual_match(self):
        for i in range(30):
            self.store.put_memory('entry-'+str(i),'далеко '*900+'Аэропорт рядом. '+str(i),'test',0)
        packet = self.store.packet(self.context,query='аэропорт')
        self.assertLessEqual(len(packet['memory']),MAX_FACTS)
        self.assertTrue(packet['retrieval']['hasMore'])
        self.assertLessEqual(len(encoded(packet['memory']).encode()),MEMORY_BYTES)
        self.assertTrue(all(m['bodyTruncated'] and 'Аэропорт' in m['body'] for m in packet['memory']))
        self.assertEqual(len(self.store.search_memory('entry-1')[0]['body']),len('далеко '*900+'Аэропорт рядом. 1'))
        for i in range(12): self.store.append(self.context,'user','а'*2000,'test')
        bounded = self.store.packet(self.context,2048,query='аэропорт')
        self.assertLessEqual(len(encoded(bounded).encode()),2048)

    def test_real_query_plan_and_no_all_memory_select(self):
        self.store.put_memory('memory','Аэропорт','test',0)
        with self.store.connect() as db:
            plan = [r['detail'] for r in db.execute('EXPLAIN QUERY PLAN '+self.store.SEARCH_SQL,
                (match_expression(query_terms('аэропорт'),self.context),'global',self.context,9))]
            self.assertTrue(any('VIRTUAL TABLE INDEX' in p and 'M' in p for p in plan),plan)
            self.assertTrue(any('SEARCH m USING INTEGER PRIMARY KEY' in p for p in plan),plan)
            self.assertFalse(any('SCAN m ' in p for p in plan),plan)
        with patch.object(self.store,'memories',side_effect=AssertionError('full list forbidden')):
            self.assertTrue(self.store.packet(self.context,query='аэропорт')['memory'])
            coordinator = Coordinator(self.store,{})
            self.assertTrue(coordinator.call_tool('memory_read',{'query':'аэропорт'},self.row('аэропорт')))

    def test_explicit_empty_tool_read_is_bounded(self):
        for i in range(30): self.store.put_memory('fact-'+str(i),'Факт '+str(i),'test',0)
        result = Coordinator(self.store,{}).call_tool('memory_read',{'query':''},self.row('привет'))
        self.assertEqual(len(result),12)

    def test_knowledge_pages_use_active_index_without_offsets_or_scope_leaks(self):
        for i in range(85):
            self.store.put_memory('fact-'+str(i),'Знание '+str(i),'test',0,self.context if i%2 else None)
        self.store.put_memory('hidden','PRIVATE OTHER TOPIC','test',0,self.other)
        self.store.put_memory('fact-4','','test',1,deleted=True)
        seen=[];cursor=None
        while True:
            page=self.store.memory_page(self.context,limit=13,after=cursor)
            self.assertLessEqual(len(page['items']),13)
            seen.extend((r['scope'],r['key']) for r in page['items'])
            cursor=page['next']
            if cursor is None:break
        self.assertEqual(len(seen),84)
        self.assertEqual(seen,sorted(set(seen)))
        self.assertNotIn((self.other,'hidden'),seen)
        with self.store.connect() as db:
            plan=[r['detail'] for r in db.execute('''EXPLAIN QUERY PLAN SELECT * FROM memory
                WHERE scope IN (?,?) AND deleted=0 AND (scope,key)>(?,?) ORDER BY scope,key LIMIT ?''',
                ('global',self.context,'global','fact-10',14))]
            self.assertTrue(any('SEARCH memory USING INDEX memory_active' in p for p in plan),plan)

    def test_tool_save_indexes_original_prompt_without_exposing_keywords(self):
        row = self.row('Мой проверочный код — береза-16.')
        result = Coordinator(self.store,{}).call_tool('memory_put',
            {'key':'verification_code','body':'береза-16','global':True,'revision':0},row)
        self.assertNotIn('keywords',result)
        self.assertEqual(self.store.search_memory('проверочный код')[0]['body'],'береза-16')

    def test_legacy_migration_backfills_alias_without_changing_fact_revision(self):
        root = Path(self.tmp.name)/'legacy';root.mkdir(mode=0o700)
        path = root/'agent-hub.db';path.touch(mode=0o600)
        context, task = str(uuid.uuid4()),str(uuid.uuid4())
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE memory(scope TEXT NOT NULL,key TEXT NOT NULL,body TEXT NOT NULL,
                source TEXT NOT NULL,revision INTEGER NOT NULL,deleted INTEGER NOT NULL,updated_at INTEGER NOT NULL,
                PRIMARY KEY(scope,key));
                CREATE TABLE tasks(id TEXT PRIMARY KEY,context_id TEXT,client TEXT,provider TEXT,prompt TEXT,
                status TEXT,output TEXT,error TEXT,native_id TEXT,created_at REAL,updated_at REAL);
                CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,context_id TEXT,kind TEXT,task_id TEXT,data TEXT,created_at REAL);''')
            db.execute("INSERT INTO memory VALUES('global','verification_code','синтетика-73','ollama',4,0,123)")
            db.execute("INSERT INTO memory VALUES('global','deleted','аэропорт','test',1,1,123)")
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (task,context,'test','ollama','Мой проверочный код — синтетика-73.','completed','','',None,1,1))
            db.execute('INSERT INTO events(context_id,kind,task_id,data,created_at) VALUES(?,?,?,?,?)',
                       (context,'memory.changed',task,encoded({'scope':'global','key':'verification_code'}),1))
        migrated = RuntimeStore(root)
        fact = migrated.search_memory('Какой мой проверочный код?')[0]
        self.assertEqual((fact['revision'],fact['updated_at']),(4,123))
        self.assertEqual(RuntimeStore(root).search_memory('аэропорт'),[])
        with migrated.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM memory_fts').fetchone()[0],1)

    def test_all_provider_contexts_use_index_and_emit_selection(self):
        self.store.put_memory('audio','гарнитура','test',0)
        self.store.put_memory('unrelated','PRIVATE UNRELATED метеорит','test',0)
        row = self.row('Какая гарнитура?');coordinator = Coordinator(self.store,{})
        payload = encoded(messages(row,coordinator))
        self.assertIn('audio',payload);self.assertNotIn('PRIVATE UNRELATED',payload)
        # Exercise actual Hermes subprocess adapter with a deterministic JSON echo child.
        program = "import json,sys; p=json.load(sys.stdin); print(json.dumps({'kind':'result','text':json.dumps(p['context'])}))"
        provider = CommandProvider([sys.executable,'-c',program],'fixture')
        with patch.object(provider,'status',return_value={'ready':True}):
            reply = provider.run(row,coordinator,threading.Event(),lambda _:None)
        self.assertEqual([r['key'] for r in json.loads(reply)['memory']],['audio'])
        captured = []
        def ao_reply(url, body=None, **_):
            if body:
                captured.append(body)
                return {'session':{'id':'fixture-session'}}
            return {'turns':[{'state':'completed'}],'messages':[{'role':'assistant','text':'готово'}]}
        cfg = dict(url='http://example.invalid',mcp_python='python',mcp_client='helper',
                   hub_url='http://127.0.0.1',token_file='private-file')
        with patch('hub_providers.request_json',side_effect=ao_reply):
            self.assertEqual(OrchestratorProvider(cfg).run(row,coordinator,threading.Event(),lambda _:None),'готово')
        self.assertNotIn('PRIVATE UNRELATED',captured[0]['prompt'])
        self.assertNotIn('{"query":""}',captured[0]['prompt'])
        self.assertEqual(len([e for e in self.store.events() if e['kind']=='memory.retrieved']),3)


if __name__ == '__main__': unittest.main()
