"""Native Codex reports and explicit voice continuations, independent of the LLM.

Observation uses only read RPCs. A separate writer is opened only after a user
message; it never changes permissions, approves tools, or retries a mutation.
Terminal turns enter the same isolated incident/outbox pipeline as webhooks.
"""
import json
import threading
import time
import uuid
from agent_hub import Conflict, encoded, text, uid
from codex_context_bridge import CodexReader, BridgeError, READ_METHODS

TERMINAL = {'completed', 'failed', 'interrupted'}
LABELS = {'completed':'завершена', 'failed':'завершилась с ошибкой',
          'interrupted':'остановлена', 'inProgress':'выполняется', 'unknown':'статус неизвестен'}


def command(prompt):
    """Only explicit Codex commands bypass the conversational model."""
    raw = prompt.strip()
    value = raw.casefold().replace('ё','е').rstrip('.!?')
    if value in {'задачи кодекса','список задач кодекса','какие задачи кодекса','задачи codex'}:
        return 'list', ''
    for prefix in ('выбери задачу кодекса ', 'выбери чат кодекса '):
        if value.startswith(prefix):
            return 'select', raw[len(prefix):].strip()
    for prefix, action in (('статус кодекса','status'),('отчет кодекса','report'),
                           ('прочитай отчет кодекса','report'),('что сделал кодекс','report')):
        if value == prefix:
            return action, ''
        if value.startswith(prefix+' по задаче '):
            return action, raw[len(prefix+' по задаче '):].strip()
    for prefix in ('передай кодексу ', 'скажи кодексу ', 'codex '):
        if value.startswith(prefix):
            return 'send', raw[len(prefix):].strip()
    return None


def turn_report(turn):
    """Commentary isn't a final answer; failures/cancellations aren't success."""
    status = turn.get('status')
    if status not in TERMINAL | {'inProgress'}:
        status = 'unknown'
    items = turn.get('items', [])
    final = [i.get('text','') for i in items if i.get('type')=='agentMessage'
             and i.get('phase') in (None,'final_answer') and isinstance(i.get('text'),str)]
    error = turn.get('error') or {}
    body = '\n'.join(final).encode()[:12000].decode('utf-8','ignore')
    paths = []
    for item in items:
        if item.get('type')=='fileChange':
            paths.extend(c.get('path','') for c in item.get('changes',[]) if isinstance(c,dict) and isinstance(c.get('path'),str))
    return {'turn_id':text(turn.get('id'),256), 'status':status, 'report':body,
            'error':str(error.get('message',''))[:1200],
            'files':[p[:300] for p in dict.fromkeys(paths) if isinstance(p,str) and p][:20]}


class CodexWriter(CodexReader):
    allowed_methods = READ_METHODS | {'thread/resume','turn/start'}

    def notification(self, value):
        if not hasattr(self,'pending'):
            self.pending = []
        if len(self.pending)<200:
            self.pending.append(value)

    def next_event(self, deadline):
        if getattr(self,'pending',None):
            return self.pending.pop(0)
        return self.receive(deadline)


class CodexLink:
    def __init__(self, store, incidents, config, reader_factory=None, writer_factory=None):
        self.store, self.incidents = store, incidents
        self.config = dict(config or {})
        self.enabled = bool(self.config.get('binary'))
        self.host = text(self.config.get('host','local'),128)
        if self.config.get('targets','auto') not in {'auto','phone','desktop','both'}:
            raise ValueError('Invalid Codex notification targets')
        if type(self.config.get('poll_seconds',15)) is not int or not 5<=self.config.get('poll_seconds',15)<=60:
            raise ValueError('Codex poll_seconds must be 5..60')
        self.reader_factory = reader_factory or CodexReader
        self.writer_factory = writer_factory or CodexWriter
        self.reader = None
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.writers = {}
        self.last_error = ''
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS codex_threads(
                    id TEXT PRIMARY KEY REFERENCES chats(id),host TEXT NOT NULL,
                    native_id TEXT NOT NULL,title TEXT NOT NULL,updated_at REAL NOT NULL,
                    turn_id TEXT,status TEXT NOT NULL,report TEXT NOT NULL,error TEXT NOT NULL,
                    files TEXT NOT NULL,observed_at REAL NOT NULL,UNIQUE(host,native_id));
                CREATE INDEX IF NOT EXISTS codex_recent ON codex_threads(host,updated_at DESC);
                CREATE TABLE IF NOT EXISTS codex_read_errors(
                    host TEXT NOT NULL,native_id TEXT NOT NULL,error TEXT NOT NULL,
                    next_attempt REAL NOT NULL,PRIMARY KEY(host,native_id));
                CREATE TABLE IF NOT EXISTS codex_selection(
                    client TEXT NOT NULL,context_id TEXT NOT NULL REFERENCES contexts(id),
                    chat_id TEXT NOT NULL REFERENCES codex_threads(id),PRIMARY KEY(client,context_id));
                CREATE TABLE IF NOT EXISTS codex_commands(
                    id TEXT PRIMARY KEY,chat_id TEXT NOT NULL REFERENCES codex_threads(id),
                    client TEXT NOT NULL,context_id TEXT NOT NULL REFERENCES contexts(id),
                    prompt TEXT NOT NULL,status TEXT NOT NULL,native_turn TEXT,error TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS codex_outbox(
                    id TEXT PRIMARY KEY,payload TEXT NOT NULL,title TEXT NOT NULL,
                    submitted INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS codex_pending_events ON codex_outbox(submitted,created_at);
            ''')
            db.execute("UPDATE codex_commands SET status='unknown',error='Hub перезапущен; сообщение не повторено.' WHERE status IN ('queued','running')")
        key='codex-enabled-at:'+self.host
        self.enabled_at=store.setting(key)
        if self.enabled and self.enabled_at is None:
            self.enabled_at=time.time()
            store.set_setting(key,self.enabled_at)

    def _connection(self, factory):
        return factory(binary=self.config['binary'],socket_path=self.config.get('socket'),timeout=15)

    def list(self, query=''):
        query=text(query,160,empty=True).casefold().replace('ё','е')
        with self.store.connect() as db:
            rows=db.execute('SELECT * FROM codex_threads WHERE host=? ORDER BY updated_at DESC LIMIT 100',(self.host,)).fetchall()
        return [{k:r[k] for k in ('id','host','native_id','title','status','updated_at','observed_at')}
                for r in rows if not query or query in r['title'].casefold().replace('ё','е') or query in (r['id'],r['native_id'])]

    def report(self, chat_id):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM codex_threads WHERE id=? AND host=?',(uid(chat_id),self.host)).fetchone()
            read_error=db.execute('SELECT error FROM codex_read_errors WHERE host=? AND native_id=?',(self.host,row['native_id'])).fetchone() if row else None
        if row is None:
            raise ValueError('Unknown Codex chat on this host')
        return {**dict(row),'files':json.loads(row['files']),'read_error':read_error['error'] if read_error else '',
                'stale':time.time()-row['observed_at']>max(60,self.config.get('poll_seconds',15)*3)}

    def observe(self, summary, turn):
        native=text(summary.get('id'),256)
        # A missing name must not invent a title from private message text.
        title=(summary.get('name') or 'Без названия')[:160]
        updated=float(summary.get('updatedAt') or 0)
        report=turn_report(turn) if turn else {
            'turn_id':None,'status':'unknown','report':'','error':'','files':[]}
        chat=self.store.register_chat(self.host,'codex',native,title,summary.get('cwd') or '')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT turn_id,status,updated_at FROM codex_threads WHERE id=?',(chat['id'],)).fetchone()
            if old and (updated<old['updated_at'] or (old['turn_id']==report['turn_id'] and old['status'] in TERMINAL and report['status'] not in TERMINAL)):
                return chat['id']
            db.execute('''INSERT INTO codex_threads VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,updated_at=excluded.updated_at,
                turn_id=excluded.turn_id,status=excluded.status,report=excluded.report,error=excluded.error,
                files=excluded.files,observed_at=excluded.observed_at''',
                (chat['id'],self.host,native,title,updated,report['turn_id'],report['status'],
                 report['report'],report['error'],encoded(report['files']),time.time()))
            changed=old and (old['turn_id'],old['status'])!=(report['turn_id'],report['status'])
            new=not old and self.enabled_at is not None and float(summary.get('createdAt') or 0)>=self.enabled_at
            if report['status'] in TERMINAL and (changed or new):
                event_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'codex:'+self.host+':'+native+':'+report['turn_id']+':'+report['status']))
                payload=encoded({'version':1,'type':'codex.turn.'+report['status'],'host':self.host,
                    'chat_id':chat['id'],'thread_id':native,'title':title,**report})
                event_title=('Codex: '+title+' — '+LABELS[report['status']])[:160]
                db.execute('INSERT OR IGNORE INTO codex_outbox(id,payload,title,created_at) VALUES(?,?,?,?)',
                           (event_id,payload,event_title,time.time()))
            if report['status'] in TERMINAL:
                db.execute('UPDATE codex_commands SET status=?,error=? WHERE chat_id=? AND native_turn=? AND status IN (\'queued\',\'running\',\'unknown\')',
                           (report['status'],report['error'],chat['id'],report['turn_id']))
        return chat['id']

    def _turn_page(self, params, summary):
        try:
            return self.reader.call('thread/turns/list',params)
        except BridgeError as exc:
            # Some old/unsupported rollouts stall or exceed the frame budget.
            # Quarantine that thread, retain its old report, and continue others.
            with self.store.connect() as db:
                db.execute('INSERT INTO codex_read_errors VALUES(?,?,?,?) ON CONFLICT(host,native_id) DO UPDATE SET error=excluded.error,next_attempt=excluded.next_attempt',
                           (self.host,summary['id'],str(exc)[:240],time.time()+300))
                exists=db.execute('SELECT 1 FROM codex_threads WHERE host=? AND native_id=?',(self.host,summary['id'])).fetchone()
            if not exists:self.observe(summary,None)
            self.reader.close();self.reader=None
            self.reader=self._connection(self.reader_factory)
            return None

    def scan(self):
        """Indexed recent-thread listing; full items only for changed/active turns.

        A bounded prefix (1000 threads) prevents an unbounded cold-start history
        scan. Status only concerns turns visible to the connected app-server.
        """
        with self.lock:
            if not self.reader:
                self.reader=self._connection(self.reader_factory)
            cursor=None
            for _ in range(10):
                page=self.reader.list_chats(100,cursor)
                unchanged=True
                for summary in page['data']:
                    if self.stop.is_set():
                        return
                    with self.store.connect() as db:
                        old=db.execute('SELECT * FROM codex_threads WHERE host=? AND native_id=?',
                                       (self.host,summary['id'])).fetchone()
                        unreadable=db.execute('SELECT next_attempt FROM codex_read_errors WHERE host=? AND native_id=?',(self.host,summary['id'])).fetchone()
                    if unreadable and unreadable['next_attempt']>time.time():
                        continue
                    if not unreadable and old and old['updated_at']==summary.get('updatedAt') and (old['status'] in TERMINAL or old['turn_id'] is None):
                        with self.store.connect() as db:
                            db.execute('UPDATE codex_threads SET observed_at=? WHERE id=?',(time.time(),old['id']))
                        continue
                    unchanged=False
                    # Overlap the last known turn: multiple short turns can finish
                    # between polls. Walk bounded new pages rather than dropping
                    # everything except the latest answer.
                    candidates=[];turn_cursor=None;read_failed=False
                    for _ in range(20 if old else 1):
                        params={'threadId':summary['id'],'limit':5 if old else 1,
                                'itemsView':'notLoaded','sortDirection':'desc'}
                        if turn_cursor:params['cursor']=turn_cursor
                        page_turns=self._turn_page(params,summary)
                        if page_turns is None:
                            read_failed=True;break
                        selected=[];found=False
                        for item in page_turns.get('data',[]):
                            selected.append(item)
                            if old and item.get('id')==old['turn_id']:
                                found=True;break
                        needs_full=[t['id'] for t in selected if t.get('status') in TERMINAL and
                                    (not old or (t.get('id'),t.get('status'))!=(old['turn_id'],old['status']))]
                        if needs_full:
                            full=self._turn_page({**params,'itemsView':'full'},summary)
                            if full is None:
                                read_failed=True;break
                            by_id={t['id']:t for t in full.get('data',[])}
                            selected=[by_id.get(t['id'],t) for t in selected]
                        candidates.extend(selected)
                        if found or not old or not page_turns.get('nextCursor'):
                            break
                        turn_cursor=page_turns['nextCursor']
                    if read_failed:continue
                    if candidates:
                        for item in reversed(candidates):
                            if old and (item.get('id'),item.get('status'))==(old['turn_id'],old['status']) and item.get('status') in TERMINAL:
                                continue
                            self.observe(summary,item)
                    else:
                        self.observe(summary,None)
                    with self.store.connect() as db:
                        db.execute('DELETE FROM codex_read_errors WHERE host=? AND native_id=?',(self.host,summary['id']))
                        db.execute('UPDATE codex_threads SET observed_at=?,updated_at=MAX(updated_at,?) WHERE host=? AND native_id=?',
                                   (time.time(),float(summary.get('updatedAt') or 0),self.host,summary['id']))
                if unchanged or not page.get('nextCursor'):
                    break
                cursor=page['nextCursor']
            self.last_error=''

    def flush(self):
        with self.store.connect() as db:
            rows=db.execute('SELECT * FROM codex_outbox WHERE submitted=0 ORDER BY created_at LIMIT 20').fetchall()
        for row in rows:
            self.incidents.accept(row['payload'].encode(),'application/json','codex-monitor:'+self.host,
                                  'codex',row['title'],self.config.get('targets','auto'),row['id'])
            with self.store.connect() as db:
                db.execute('UPDATE codex_outbox SET submitted=1 WHERE id=?',(row['id'],))

    def _selected(self, client, context, query=''):
        if query:
            rows=self.list(query)
        else:
            with self.store.connect() as db:
                row=db.execute('SELECT chat_id FROM codex_selection WHERE client=? AND context_id=?',(client,context)).fetchone()
            rows=[self.report(row['chat_id'])] if row else []
        if len(rows)!=1:
            if rows:
                return None,'Подходят несколько задач: '+', '.join(r['title'] for r in rows[:5])+'. Назовите точнее.'
            return None,'Выберите задачу: скажите «задачи кодекса», затем «выбери задачу кодекса» и её название.'
        return self.report(rows[0]['id']),''

    def voice(self, prompt, client, context, request_id):
        parsed=command(prompt)
        if parsed is None:
            return None
        text(client,128);uid(context);uid(request_id)
        if not self.enabled:
            return 'Мост Codex ещё не подключён к Hub.'
        action,query=parsed
        if action=='list':
            rows=self.list()
            if not rows:
                return 'Список задач Codex пока пуст. Мост ещё не получил данные.'
            return 'Задачи Codex: '+'; '.join(r['title']+' — '+LABELS.get(r['status'],'статус неизвестен') for r in rows[:8])+'. Для выбора скажите «выбери задачу кодекса» и название.'
        row,question=self._selected(client,context,query if action!='send' else '')
        if not row:
            return question
        if action=='select':
            with self.store.connect() as db:
                db.execute('INSERT INTO codex_selection VALUES(?,?,?) ON CONFLICT(client,context_id) DO UPDATE SET chat_id=excluded.chat_id',
                           (client,context,row['id']))
            return 'Выбрана задача «'+row['title']+'». Скажите «статус кодекса», «прочитай отчёт кодекса» или «передай кодексу» и сообщение.'
        if action=='send':
            value=self.send(row['id'],query,client,context,request_id)
            return ('Сообщение принято мостом для задачи «'+row['title']+'». Codex ещё не подтвердил выполнение. Отчёт придёт отдельно.'
                    if value['status'] in {'queued','running'} else 'Состояние сообщения: '+LABELS.get(value['status'],value['status'])+'. '+value['error'])
        prefix='Задача «'+row['title']+'» '+LABELS.get(row['status'],'имеет неизвестный статус')+'.'
        with self.store.connect() as db:
            pending=db.execute('''SELECT status,error FROM codex_commands WHERE chat_id=? AND client=? AND context_id=?
                ORDER BY created_at DESC LIMIT 1''',(row['id'],client,context)).fetchone()
        if pending and pending['status'] in {'queued','running','unknown','failed'}:
            prefix+=' Последнее сообщение: '+{'queued':'ожидает отправки','running':'выполняется','unknown':'результат неизвестен','failed':'не выполнено'}[pending['status']]+'. '+pending['error']
        if row['stale']:
            prefix+=' Это сохранённый статус: связь с Codex давно не обновлялась.'
        if row['read_error']:
            prefix+=' Чтение этой задачи сейчас недоступно.'
        if action=='report':
            body=row['report'] or row['error'] or 'Итогового отчёта пока нет.'
            prefix+=' '+body[:1200]
            if len(body)>1200:
                prefix+=' Полный отчёт сохранён в Hub.'
        return prefix[:1500]

    def send(self, chat_id, prompt, client, context, request_id):
        """Persist before native mutation. A lost response must not duplicate work."""
        text(prompt,2000);text(client,128);uid(context);uid(request_id)
        row=self.report(chat_id)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.store.require_context(db,context)
            previous=db.execute('SELECT * FROM codex_commands WHERE id=?',(request_id,)).fetchone()
            if previous:
                if (previous['chat_id'],previous['client'],previous['context_id'],previous['prompt'])!=(chat_id,client,context,prompt):
                    raise Conflict('Codex command ID conflict')
                return dict(previous)
            active=db.execute("SELECT 1 FROM codex_commands WHERE chat_id=? AND status IN ('queued','running','unknown')",(chat_id,)).fetchone()
            if active:
                raise Conflict('Предыдущее сообщение ещё выполняется или его результат неизвестен. Проверьте Codex.')
            db.execute('INSERT INTO codex_commands VALUES(?,?,?,?,?,\'queued\',NULL,\'\',?)',
                       (request_id,chat_id,client,context,prompt,time.time()))
        threading.Thread(target=self._send,args=(request_id,row,prompt),daemon=True,name='codex-continuation').start()
        return {'id':request_id,'status':'queued'}

    def commands(self, client, context):
        text(client,128);uid(context)
        with self.store.connect() as db:
            self.store.require_context(db,context)
            return [dict(r) for r in db.execute('SELECT * FROM codex_commands WHERE client=? AND context_id=? ORDER BY created_at DESC LIMIT 30',(client,context))]

    def acknowledge_unknown(self, request_id, client, context):
        """Owner checked Codex manually; release the guard without retrying it."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM codex_commands WHERE id=? AND client=? AND context_id=?',(uid(request_id),text(client,128),uid(context))).fetchone()
            if row is None:raise ValueError('Unknown Codex command')
            if row['status'] not in {'unknown','acknowledged'}:raise Conflict('Only an uncertain command can be acknowledged')
            db.execute("UPDATE codex_commands SET status='acknowledged' WHERE id=?",(request_id,))
        return {'id':request_id,'status':'acknowledged','retried':False}

    def _command_update(self, request_id, status, error='', native_turn=None):
        with self.store.connect() as db:
            db.execute('UPDATE codex_commands SET status=?,error=?,native_turn=COALESCE(?,native_turn) WHERE id=?',
                       (status,error[:1200],native_turn,request_id))
            if status in {'unknown','failed'}:
                row=db.execute('''SELECT c.chat_id,t.title,t.native_id FROM codex_commands c
                    JOIN codex_threads t ON t.id=c.chat_id WHERE c.id=?''',(request_id,)).fetchone()
                event_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'codex-message:'+request_id+':'+status))
                payload=encoded({'version':1,'type':'codex.message.'+status,'host':self.host,
                    'chat_id':row['chat_id'],'thread_id':row['native_id'],'command_id':request_id,
                    'status':status,'error':error[:1200]})
                db.execute('INSERT OR IGNORE INTO codex_outbox(id,payload,title,created_at) VALUES(?,?,?,?)',
                           (event_id,payload,('Codex: '+row['title']+' — сообщение не подтверждено')[:160],time.time()))

    def _send(self, request_id, row, prompt):
        mutated=False
        try:
            with self._connection(self.writer_factory) as writer:
                with self.lock:
                    self.writers[request_id]=writer
                # No permission/cwd/model overrides. Persisted session policy remains.
                recent=writer.call('thread/turns/list',{'threadId':row['native_id'],'limit':1,'itemsView':'full'})
                if any(t.get('status')=='inProgress' for t in recent.get('data',[])):
                    self._command_update(request_id,'failed','Задача уже выполняется. Дождитесь завершения перед новым сообщением.')
                    return
                writer.call('thread/resume',{'threadId':row['native_id'],'excludeTurns':True})
                mutated=True
                turn=writer.call('turn/start',{'threadId':row['native_id'],'clientUserMessageId':request_id,
                    'input':[{'type':'text','text':prompt,'text_elements':[]}]}).get('turn',{})
                text(turn.get('id'),256)
                self._command_update(request_id,'running',native_turn=turn.get('id'))
                deadline=time.monotonic()+3600
                while not self.stop.is_set() and time.monotonic()<deadline:
                    try:
                        event=writer.next_event(min(deadline,time.monotonic()+1))
                    except BridgeError as exc:
                        if 'timed out' in str(exc):
                            continue
                        raise
                    if 'method' in event and 'id' in event:
                        # This bridge cannot approve shell, file writes or permissions.
                        writer.send({'id':event['id'],'error':{'code':-32601,'message':'Approve in the owning Codex client'}})
                    elif (event.get('method')=='turn/completed' and event.get('params',{}).get('threadId')==row['native_id']
                          and event['params'].get('turn',{}).get('id')==turn.get('id')):
                        completed=event['params']['turn']
                        # Some protocol versions send terminal items separately.
                        try:
                            page=writer.call('thread/turns/list',{'threadId':row['native_id'],'limit':1,'itemsView':'full'})
                            latest=next((t for t in page.get('data',[]) if t.get('id')==turn.get('id')),completed)
                        except BridgeError:
                            latest=completed
                        latest={**latest,'status':completed['status'],'error':completed.get('error')}
                        self.observe({'id':row['native_id'],'name':row['title'],'updatedAt':time.time()},latest)
                        return
                self._command_update(request_id,'unknown','Соединение завершилось; результат неизвестен. Повтора нет.')
        except Exception as exc:
            self._command_update(request_id,'unknown' if mutated else 'failed',
                'Мост Codex не получил подтверждения ('+type(exc).__name__+'). Автоматического повтора нет. Проверьте задачу в Codex.')
        finally:
            with self.lock:
                self.writers.pop(request_id,None)

    def start(self):
        if not self.enabled or self.thread:
            return
        def loop():
            while not self.stop.is_set():
                try:
                    self.flush()
                    self.scan()
                    self.flush()
                except Exception as exc:
                    self.last_error=type(exc).__name__
                    with self.lock:
                        if self.reader:
                            self.reader.close();self.reader=None
                self.stop.wait(max(5,min(60,self.config.get('poll_seconds',15))))
        self.thread=threading.Thread(target=loop,daemon=True,name='codex-observer')
        self.thread.start()

    def close(self):
        self.stop.set()
        with self.lock:
            if self.reader:
                self.reader.close();self.reader=None
        if self.thread:
            self.thread.join(timeout=2)
