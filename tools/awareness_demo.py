#!/usr/bin/env python3
"""Disposable local prototype, synthetic data and separate context owner stores."""
import argparse
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))


def owner(directory):
    request=json.load(sys.stdin)
    # This separate process represents the authority owning history, outside Hub.
    folder=Path(directory);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    with sqlite3.connect(folder/'owner.db') as db:
        db.execute('CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,session TEXT,body TEXT)')
        db.execute('INSERT OR IGNORE INTO turns VALUES(?,?,?)',(request['id'],request['session'],request['text']))
        count=db.execute('SELECT count(*) FROM turns WHERE session=?',(request['session'],)).fetchone()[0]
    reply=('Демо владельца «'+request['owner']+'». Сообщений в этом контексте: '+str(count)+
           '. Получено: '+request['text']+'. Ссылки: '+json.dumps(request['context_refs'],ensure_ascii=False)+
           '. Историю соседних контекстов не получал. Это проверка маршрута, без нейросетевого ответа.')
    print(json.dumps({'id':request['id'],'reply':reply[:1500]},ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-state')
    parser.add_argument('--port',type=int,default=0)
    args=parser.parse_args()
    if args.owner_state:
        owner(args.owner_state);return
    from hub_server import HubApplication,HubHandler
    from hub_awareness import demo_events
    with tempfile.TemporaryDirectory(prefix='assistant-awareness-demo-') as tmp:
        root=Path(tmp);token=secrets.token_urlsafe(48)
        (root/'token').write_text(token);(root/'token').chmod(0o600)
        routes=[]
        for name,label,aliases in [('work','Работа',['работа','работе','рабочий']),('project','Свой проект',['проект','проекте','deadnodes'])]:
            routes.append({'id':name,'label':label,'owner':'demo-'+name,'aliases':aliases,
                           'context_refs':{'product_id':'demo-'+name,'feature_id':'demo-'+name+'-feature'},
                           'argv':[sys.executable,str(Path(__file__).resolve()),'--owner-state',str(root/('owner-'+name))]})
        app=HubApplication({'state':str(root/'hub'),'token_file':str(root/'token'),'providers':{},
                            'awareness_demo':True,'context_router':{'routes':routes}})
        # Synthetic paired device is an inbox database entry, never a physical phone.
        device=str(uuid.uuid4());app.mobile.link.sync(device,{'version':1,'revision':1,'sources':[],'people':[],'note':None})
        for event in demo_events():app.awareness.accept(event,'synthetic-demo')
        class DemoHandler(HubHandler):
            def do_GET(self):
                if self.path=='/':self.path='/awareness'
                super().do_GET()
        server=ThreadingHTTPServer(('127.0.0.1',args.port),DemoHandler);server.app=app;server.daemon_threads=True
        origin='http://127.0.0.1:'+str(server.server_port)
        ticket=app.browser.ticket('owner',origin)
        print(json.dumps({'url':origin+'/ticket?id='+ticket,'origin':origin,'token_file':str(root/'token'),
                          'demo_device':device,'state':str(root),'note':'Synthetic only; Ctrl+C removes demo state'},ensure_ascii=False),flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()


if __name__=='__main__':main()
