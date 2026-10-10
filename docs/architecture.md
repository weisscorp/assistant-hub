# Architecture and implementation status

## Ownership of state

Assistant Hub is the canonical store for its own conversations and shared facts.
Each model request is an owned task in a topic. Native provider session IDs are
references, not replacements for the Hub's history. An existing Codex desktop
chat is not automatically a Hub topic: import its bounded context explicitly
with the read-only bridge, or create a new Hub conversation.

| Layer | Implementation | Boundary |
|---|---|---|
| UI / API | `hub_server.py`, `hub_ui/` | Loopback, bearer auth or browser login ticket |
| Coordination | `hub_runtime.py` | Per-topic serial queue, cross-topic concurrency |
| Persistence | `agent_hub.py`, SQLite WAL | History, revisions, client focus, chats, notifications |
| Retrieval | `knowledge_search.py`, SQLite FTS5 | Lexical recall, bounded prompt memory |
| Providers | `hub_providers.py` | Ollama, Codex, command/Hermes, AO, cloud |
| Agent tools | `hub_mcp.py`, `hub_mcp_client.py` | Scoped topic and optional parent task |
| Mobile | `hub_mobile.py`, `home_link.py` | Snapshot sync and polling inbox |
| Desktop mascot | `coucou_events.py` | Original Coucou socket, no copied mascot assets |

## Request path

1. A client chooses a topic and provider and submits a UUID request.
2. Hub persists the task and history entry, then queues it within the topic.
3. Code queries the knowledge index with the request's keywords and allowed scopes.
4. The provider receives bounded history, selected facts, and supported tools.
5. Tool calls run through Hub, recording results and memory revisions.
6. The actual final reply or error is persisted and emitted as a topic event.
7. Clients consume events/history; paired phones may receive an inbox message.

A notification does not silently switch every client's topic. Following one
updates only the requesting client's focus. `agent_task` delegates to Codex or
Hermes for an explicit user request; provider selection is otherwise explicit.

## Recovery and cancellation

A repeated UUID does not run a task twice. Pending/running tasks at process
restart become `unknown`: the Hub does not replay side effects. Cancellation
sets terminal state immediately; a network read may take time to return, and
late results are discarded. History and knowledge remain durable.

## Scope of this release

Core HTTP/SQLite behavior and provider protocol fixtures are covered by tests.
Ollama, Codex, Hermes and AO have been exercised in a local development stack;
that does not guarantee availability on every machine or model. Paid API calls
have not been used for validation. Coucou's model API and socket event delivery
are integrated; every visual animation/approval path is not verified.

The Android repository has local voice/tools, knowledge and inbox clients. A
TLS gateway, complete bidirectional phone action loop, fleet-level capabilities,
Home Assistant/Yandex integrations, Slack/mail ingestion, OTA automation and
Android mascot reuse are future work. Neither this repository nor its launcher
opens a running service to the Internet.
