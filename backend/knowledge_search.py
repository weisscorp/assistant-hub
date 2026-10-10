"""Bounded lexical recall. SQLite FTS5 does retrieval, never an LLM or a table scan."""
import re
import unicodedata

STOP_WORDS = frozenset('''а без бы был была были было быть в вам вас весь во вот все всего вы где да для до его ее ей если есть еще же за зачем здесь и из или им их к как какой какая какие какое когда кто ли мне мной могу может можно мы на надо наш не него нет ни но о об он она они оно от по под пока пожалуйста почему при про с сам сейчас со спасибо так там тебе тебя то только тут ты у уже что чтобы это этой этот эти я мой моя мои мое чем чём the a an and are as at be been by can could do does for from how i in is it me my of on or please that the their them they this to was we what when where which who will with would you your'''.split())
WORDS = re.compile(r'[^\W_]+', re.UNICODE)
# Deliberately small suffix heuristic, not semantic search or full morphology.
RUSSIAN_SUFFIXES = ('иями','ями','ами','ого','ему','ому','ыми','ими','ией','иях','ах','ях',
                    'ые','ие','ый','ий','ой','ая','яя','ую','юю','ое','ее','ов','ев',
                    'ом','ем','ам','ям','ы','и','а','я','у','ю','е','о')
MAX_TERMS = 12
MAX_FACTS = 8
MEMORY_BYTES = 6144
FACT_BODY_BYTES = 640


def words(value):
    value = unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')
    return [w for w in WORDS.findall(value) if w not in STOP_WORDS and (len(w) > 1 or w.isdigit())]


def keywords(value):
    """Small source-language aliases alongside the canonical key and body."""
    return ' '.join(dict.fromkeys(words(value)[-128:]))


def query_terms(value):
    terms = []
    for word in words(value):
        if re.fullmatch('[а-я]+', word):
            for suffix in RUSSIAN_SUFFIXES:
                if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                    word = word[:-len(suffix)]
                    break
        if word not in terms:
            terms.append(word)
    # A long shared prompt can start with metadata; prioritize the actual latest question.
    return terms[-MAX_TERMS:]


def match_expression(terms, context):
    if not terms:
        return None
    # Tokens contain only letters/numbers, but quoting also keeps FTS syntax literal.
    query = ' OR '.join('"' + t + '"' + ('*' if len(t) >= 3 and re.fullmatch('[а-я]+',t) else '') for t in terms)
    # One token per scope avoids UUID phrase-position processing on every lookup.
    scopes = '"global"' + (' OR "' + context.replace('-','') + '"' if context else '')
    return '{key body keywords}:(' + query + ') AND scope:(' + scopes + ')'


def excerpt(body, terms, maximum=FACT_BODY_BYTES):
    raw = body.encode('utf-8')
    if len(raw) <= maximum:
        return body, False
    folded = body.casefold().replace('ё', 'е')
    positions = [folded.find(term) for term in terms if term in folded]
    start = max(0, min(positions, default=0) - 70)
    # Keep explicit omission markers and valid UTF-8. Full data remains available by key.
    preview = body[start:].encode('utf-8')[:maximum-8].decode('utf-8', errors='ignore')
    return ('…' if start else '') + preview + '…', True
