# Shared memory, history and indexed recall

## What is stored

`state/agent-hub.db` is a SQLite WAL database in the private runtime directory.
Topics have UUIDs. Each client has an independent topic focus with a revision.
Entries record user/assistant/tool/note roles, source and timestamps. Memory
records contain a key, body, source, scope, revision, deletion tombstone and
update time. Tasks/events record input, tool results, final output, errors and
provider/session references. This is private user data, not repository content.

`global` facts can be recalled in every topic. Topic facts can be recalled only
in that topic. This is topic filtering within a single trusted owner's Hub,
not a multi-tenant permission system. All holders of the full Hub token are
trusted clients.

## Automatic saving

The tools tell the agent to save durable facts stated by the user, including
preferences, devices and accepted decisions. Personal facts default to global;
task-specific decisions are topic-local. Questions, quotes, examples, guesses
and requests not to remember are excluded by policy. The agent reads/reuses
an existing key and revision when updating a fact and skips unchanged values.

This policy still relies on model behavior. A response saying “remembered” is
not proof: check the memory row and `memory.changed` event. The database update
is the durable operation, with revision conflicts preventing lost writes.

## Automatic retrieval is implemented in code

Before inference, SQLite FTS5 searches an inverted lexical index. Its segmented
B-trees and prefix indexes avoid Python walking every fact body. Triggers update
the index in the same transaction as a fact change. A one-time migration builds
older indexes and source-language keywords without rescanning history each turn.

The indexed fields include fact keys/bodies and keywords from the original user
phrase. This supports Russian recall when the stored key is English and the
value alone has no useful search terms. Query processing normalizes `ё/е`, drops
stop words and approximates simple Russian endings with prefix matching. It is
not semantic/vector search: unrelated synonyms can be missed.

| Automatic recall limit | Value |
|---|---:|
| Query terms | 12 |
| Selected facts | 8 |
| Memory JSON budget | 6144 UTF-8 bytes |
| Excerpt per long fact | 640 UTF-8 bytes |

A no-match query produces empty memory, never the entire knowledge base. Excerpts
carry a truncation flag; explicit `memory_read` can retrieve full matching facts.
An empty tool search is bounded to 12 facts. UI polling is bounded, while the
memory pane uses keyset pagination (50 records), not full-list polling/offsets.

`memory.retrieved` records selected keys/revisions, query terms, elapsed time and
payload size. Common words can match many postings, so ranked search is not
constant time. A reproducible benchmark uses only a temporary synthetic database:

```sh
python3 tools/benchmark_knowledge.py --facts 50000 --repetitions 80
```

## Check cross-topic recall

1. In one Hub/Coucou topic, state a synthetic preference such as “Я предпочитаю
   короткие ответы”. Check whether `memory_put` actually added a global fact.
2. Open a second topic/client and ask what answer length you prefer.
3. Verify the retrieval event and the matching key/revision as well as the reply.
4. Correct the preference; the existing key should get a new revision.
5. Test a topic-only fact separately; another topic must not receive it.

Use [backup guidance](../SECURITY.md) for private data. The database is protected
by filesystem permissions; it is not encrypted by the application itself.
