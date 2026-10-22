"""File-backed terminology retrieval. Only matched entries go into model context."""
import difflib, hashlib, json, re
from pathlib import Path
from recording_archive import atomic_text, session_lock

SEED=Path(__file__).resolve().parents[1]/'data/speech-glossary.json'
DEFAULT_PATH=Path.home()/'.local/share/assistant-stack/state/speech-glossary.json'
TOKEN=re.compile(r'\b\w+\b')

def load(path=None):
    path=Path(path or DEFAULT_PATH)
    if not path.exists():
        with session_lock(path.parent,'speech-glossary'):
            if not path.exists():atomic_text(path,SEED.read_text())
    data=json.loads(path.read_text())
    if data.get('version')!=1 or not isinstance(data.get('entries'),list):raise ValueError('Invalid speech glossary')
    return data

def relevant(text,data,limit=12):
    words={w.casefold() for w in TOKEN.findall(text)};found=[]
    for entry in data['entries']:
        names=[entry['canonical'],*entry.get('aliases',[])]
        if not any(name.casefold() in words for name in names):continue
        found.append(entry)
        if len(found)>=limit:break
    return found

def edits(target,left,right,data):
    result=[];context=left+target+right;offset=len(left)
    # Exact canonical names are protected too; stable glossary wins over model guesses.
    for entry in relevant(target,data,limit=len(data['entries'])):
        names={a.casefold() for a in [entry['canonical'],*entry.get('aliases',[])]}
        for match in TOKEN.finditer(target):
            if match.group().casefold() not in names:continue
            nearby=context[max(0,offset+match.start()-280):offset+match.end()+280]
            if entry.get('context') and not re.search(entry['context'],nearby,re.I):continue
            result.append({'start':match.start(),'end':match.end(),'before':match.group(),
                           'after':entry['canonical'],'reason':'Глоссарий: '+entry['canonical'],'source':'glossary'})
    return sorted(result,key=lambda e:e['start'])

def merge(target,model_edits,known):
    # Drop an entire overlapping model edit rather than keep a speculative portion.
    kept=[e for e in model_edits if not any(e['start']<k['end'] and e['end']>k['start'] for k in known)]
    return sorted(kept+[e for e in known if e['before']!=e['after']],key=lambda e:e['start'])

def observe(path,text,accepted,session):
    """Learn recurring phonetic aliases, never trust a single model suggestion.

    New English names additionally need literal source evidence. Store hashes of
    independent sessions, never transcript excerpts, in the private glossary.
    """
    from transcript_review import normalized
    path=Path(path);evidence=hashlib.sha256(session.encode()).hexdigest()
    with session_lock(path.parent,'speech-glossary'):
        data=json.loads(path.read_text()) if path.exists() else json.loads(SEED.read_text())
        for edit in accepted:
            if edit.get('source')=='glossary':continue
            a=TOKEN.findall(edit['before']);b=TOKEN.findall(edit['after'])
            if len(a)!=len(b):continue
            for alias,canonical in zip(a,b):
                if not re.fullmatch(r'[А-Яа-яЁё]{4,40}',alias) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{2,39}',canonical):continue
                if difflib.SequenceMatcher(None,normalized(alias),canonical.casefold()).ratio()<.75:continue
                existing=next((e for e in data['entries'] if e['canonical'].casefold()==canonical.casefold()),None)
                if existing and alias.casefold() in {x.casefold() for x in existing.get('aliases',[])}:continue
                canonical=existing['canonical'] if existing else canonical
                candidates=data.setdefault('candidates',[])
                candidate=next((c for c in candidates if c['alias']==alias.casefold() and c['canonical']==canonical),None)
                if candidate is None:
                    if len(candidates)>=2000:continue
                    candidate={'alias':alias.casefold(),'canonical':canonical,'sessions':[],'literalEvidence':False,'status':'candidate'};candidates.append(candidate)
                candidate['sessions']=sorted(set(candidate['sessions']+[evidence]))[:20]
                candidate['literalEvidence'] |= bool(re.search(r'\b'+re.escape(canonical)+r'\b',text,re.I))
                conflict=any(alias.casefold() in {x.casefold() for x in e.get('aliases',[])} and e['canonical']!=canonical for e in data['entries'])
                # Both known and novel names require independent, literal confirmation.
                if len(candidate['sessions'])>=2 and candidate['literalEvidence'] and not conflict:
                    if existing is None:
                        existing={'canonical':canonical,'aliases':[],'source':'auto-evidence'};data['entries'].append(existing)
                    existing['aliases'].append(alias.casefold());candidate['status']='active'
        atomic_text(path,json.dumps(data,ensure_ascii=False,indent=2)+'\n')
