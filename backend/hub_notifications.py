"""Trusted topic notices delivered to Coucou without a model or a new incident.

Use the desktop worker's existing lease/receipt protocol. A socket write is a
submission, not proof the user saw it. Uncertain deliveries are never replayed.
"""
import time
import uuid
from agent_hub import Conflict, encoded, text, uid


class DesktopNotices:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS desktop_notice_cursor(id INTEGER PRIMARY KEY CHECK(id=1),position INTEGER NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS desktop_notice_deliveries(
                id TEXT PRIMARY KEY,notification_id TEXT NOT NULL REFERENCES notifications(id),
                client TEXT NOT NULL,status TEXT NOT NULL,lease TEXT,expires REAL,result TEXT,
                error TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,updated_at REAL NOT NULL,
                UNIQUE(notification_id,client))''')
            db.execute('CREATE INDEX IF NOT EXISTS desktop_notice_queue ON desktop_notice_deliveries(client,status,created_at)')
            # SQLite indexes include rowid: source + cursor skips historical
            # rows instead of scanning every already delivered notice per tick.
            db.execute('CREATE INDEX IF NOT EXISTS notice_source ON notifications(source)')
            # An upgrade must not suddenly show all historical timer alerts.
            db.execute('INSERT OR IGNORE INTO desktop_notice_cursor SELECT 1,COALESCE(MAX(rowid),0) FROM notifications')

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE desktop_notice_deliveries SET status='unknown',error='Нет подтверждения доставки; повтор автоматически не отправлен.',updated_at=? WHERE status='running'", (time.time(),))

    def queue(self, clients):
        desktops = sorted({text(c['id'],128) for c in clients if c['kind']=='desktop'})
        if not desktops:
            return 0
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            position = db.execute('SELECT position FROM desktop_notice_cursor WHERE id=1').fetchone()[0]
            rows = db.execute("""SELECT rowid,id FROM notifications WHERE source='hub-timer'
                AND rowid>? ORDER BY rowid LIMIT 30""",(position,)).fetchall()
            now = time.time()
            for row in rows:
                for client in desktops:
                    delivery = str(uuid.uuid5(uuid.NAMESPACE_URL,'topic-notice:'+row['id']+':'+client))
                    db.execute("INSERT INTO desktop_notice_deliveries(id,notification_id,client,status,created_at,updated_at) VALUES(?,?,?,'queued',?,?)",
                               (delivery,row['id'],client,now,now))
            if rows:
                db.execute('UPDATE desktop_notice_cursor SET position=? WHERE id=1',(rows[-1]['rowid'],))
        return len(rows)

    def claim(self, client):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            db.execute("UPDATE desktop_notice_deliveries SET status='unknown',error='Нет подтверждения доставки.',updated_at=? WHERE status='running' AND expires<?",(now,now))
            row = db.execute("""SELECT d.id,d.notification_id,n.source,n.body,n.created_at FROM desktop_notice_deliveries d
                JOIN notifications n ON n.id=d.notification_id WHERE d.client=? AND d.status='queued'
                ORDER BY d.created_at,d.id LIMIT 1""",(text(client,128),)).fetchone()
            if not row:
                return None
            lease = str(uuid.uuid4())
            db.execute("UPDATE desktop_notice_deliveries SET status='running',lease=?,expires=?,updated_at=? WHERE id=?",(lease,now+60,now,row['id']))
        return {'id':row['id'],'kind':'message','source':row['source'],'title':'Таймер завершён',
                'body':row['body'][:4000],'priority':'normal','createdAt':int(row['created_at']*1000),
                'notification_id':row['notification_id'],'lease':lease}

    def receipt(self, client, delivery_id, status, lease=None, coucou=False):
        if status not in {'submitted','blocked','failed','unknown'} or type(coucou) is not bool:
            raise ValueError('Invalid delivery receipt')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM desktop_notice_deliveries WHERE id=? AND client=?',(uid(delivery_id),client)).fetchone()
            if row is None:
                return {'tracked':False}
            if not lease or row['lease']!=uid(lease):
                raise Conflict('Delivery lease mismatch')
            result = encoded({'status':status,'coucou_written':coucou})
            if row['status'] in {'submitted','blocked','failed','unknown'}:
                if row['result']!=result:
                    raise Conflict('Receipt conflict')
                return {'tracked':True}
            if row['status']!='running' or row['expires']<time.time():
                raise Conflict('Delivery outcome is unknown')
            db.execute('UPDATE desktop_notice_deliveries SET status=?,result=?,updated_at=? WHERE id=?',(status,result,time.time(),delivery_id))
        return {'tracked':True}
