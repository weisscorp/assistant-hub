"""Dated user recordings and same-name transcripts, independent of the ASR corpus."""
from contextlib import contextmanager
from datetime import datetime
import fcntl,json,os,subprocess,tempfile
from pathlib import Path

TRACK_NAMES={'mic':'микрофон','system':'звук-Mac'}

def atomic_text(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

@contextmanager
def session_lock(archive,session):
    path=Path(archive);path.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (path/('.'+session+'.lock')).open('a') as lock:
        os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        yield

def prepare(directory,archive,session,started=None,sources=None):
    directory=Path(directory);archive=Path(archive);archive.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=directory/'archive.json'
    if path.is_file():return json.loads(path.read_text())
    if started is None:started=getattr(directory.stat(),'st_birthtime',directory.stat().st_mtime)
    stamp=datetime.fromtimestamp(started).strftime('%Y-%m-%d_%H-%M-%S')
    base=stamp+'_'+session[:8]
    inferred=sorted({file.name.split('-')[0] for file in directory.glob('*.raw.caf')})
    value={'session':session,'started':started,'base':base,'archive':str(archive),'sources':sources or inferred or ['mic','system'],'phase':'recording','error':''}
    atomic_text(path,json.dumps(value,ensure_ascii=False))
    for track in value['sources']:
        if not list(directory.glob(track+'-*.raw.caf')):continue
        txt=archive/(base+'_'+TRACK_NAMES[track]+'.txt')
        if not txt.exists():atomic_text(txt,'Транскрибация готовится.\n')
    return value

def write_partial(directory,rows):
    directory=Path(directory);meta_path=directory/'archive.json'
    if not meta_path.is_file():return
    meta=json.loads(meta_path.read_text());archive=Path(meta['archive'])
    with session_lock(archive,meta['session']):
        if meta.get('phase')=='ready' or json.loads(meta_path.read_text()).get('phase')=='ready':return
        for track in meta['sources']:
            text='\n'.join(row['text'] for row in sorted(rows,key=lambda row:(row.get('startMs',0),row['id'])) if row['origin']=='desktop-'+track)
            preview=directory/'partial-preview.json'
            if preview.is_file():
                try:
                    partial=json.loads(preview.read_text());current=partial.get('paths',{}).get(track)
                    if current and (directory/current).is_file() and partial.get('tracks',{}).get(track):
                        text+='\n\nТекущая фраза (предварительно): '+partial['tracks'][track]
                except ValueError:pass
            atomic_text(archive/(meta['base']+'_'+TRACK_NAMES[track]+'.txt'),'Предварительная транскрибация · запись/обработка продолжается\n\n'+text+'\n')

def export_audio(directory):
    directory=Path(directory);meta=json.loads((directory/'archive.json').read_text());archive=Path(meta['archive']);paths=[]
    for track in meta['sources']:
        chunks=sorted(directory.glob(track+'-*.raw.caf'))
        if not chunks:continue
        # Refuse an invalid concatenation if a device changed its native format mid-session.
        formats=set()
        for chunk in chunks:
            metadata=chunk.with_suffix('.json')
            if metadata.is_file():
                captured=json.loads(metadata.read_text());formats.add((captured.get('sampleRate'),captured.get('channels')))
        if len(formats)>1:raise ValueError('Формат устройства изменился во время записи. Исходные CAF-части сохранены; их нужно экспортировать отдельно.')
        # CAF chunks retain their native PCM; stream copy preserves samples/rate/channels.
        listing=directory/(track+'-concat.txt')
        atomic_text(listing,''.join("file '"+str(path).replace("'","'\\''")+"'\n" for path in chunks))
        target=archive/(meta['base']+'_'+TRACK_NAMES[track]+'.wav');temporary=target.with_suffix('.building.wav')
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing),'-map','0:a:0','-c:a','copy','-rf64','auto',str(temporary)],check=True,capture_output=True)
        temporary.chmod(0o600);os.replace(temporary,target);paths.append((track,target))
    return paths
