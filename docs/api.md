# API and extension points

Hub's default origin is `http://127.0.0.1:8778`. Authenticated requests send
`Authorization: Bearer <private-token>`. Browser login uses an HttpOnly,
SameSite cookie, normally obtained through `tools/local_stack.py open`.
Host/Origin checks reject arbitrary origins. This is a single-owner API.
Named, revocable client keys and a trusted HTTPS reverse proxy are supported;
see [API keys and Internet access](internet-access.md). Secrets belong in the
Bearer header, not a URL. All keys have full owner access.

| Method/path | Role |
|---|---|
| GET `/health` | Authenticated health |
| GET `/api/state?client=NAME` | Providers, topics, client focus and bounded memory |
| POST `/api/contexts` | Create a topic with `title` |
| POST `/api/focus` | `client`, `context_id`, expected `revision` |
| POST `/api/messages` | UUID `id`, `context_id`, `client`, `provider`, `prompt` |
| GET `/api/tasks/UUID` | Durable task state/output/error |
| POST `/api/cancel` | Cancel task `id` |
| GET `/api/nodes?query=TEXT&schemas=1` | Nodes and bounded advertised schemas |
| GET `/api/tools?task=UUID&context=UUID` | Task-bound generic and typed node tools |
| POST `/api/nodes` | Register `id`, `label`, `kind`, `capabilities`, optional `ttl` |
| POST `/api/nodes/ID/claim` | Empty object; atomically claim a queued call and renew heartbeat |
| POST `/api/nodes/ID/result` | Acknowledge `id`, `lease`, `result`, optional `error` |
| POST `/api/capabilities/call` | UUID `id`, `node`, `capability`, `arguments`, `context_id`, `client` |
| GET `/api/capabilities/calls/UUID?context=UUID` | Actual call outcome |
| GET `/api/timers?context=UUID` | Current-topic persisted timers |
| GET `/api/events?after=SEQ&context=UUID` | Incremental event consumption |
| GET `/api/history?context=UUID&before=SEQ` | Bounded history page |
| GET `/api/context?id=UUID&query=TEXT` | Automatically selected knowledge packet |
| GET/POST `/api/memory` | Paginated facts / revisioned write |
| POST `/api/tool` | Hub tool invocation scoped to context/client/parent task |
| POST `/api/notify` | Topic `context_id`, `source`, `body` |
| POST `/api/follow` | Follow notification for one client with expected revision |
| GET `/v1/models` | Provider/topic model aliases |
| POST `/v1/chat/completions` | OpenAI-compatible facade (including Coucou) |
| POST `/conversation` | Mobile text turn |
| `/link/DEVICE_UUID/...` | Mobile sync, images, events and acknowledgements |

See `hub_server.py` and HTTP tests for exact validation and response fields.
Without `query`, `/api/context` is an explicit manual export; automatic provider
inference always supplies the current request for indexed recall. The restricted
`/coucou/CAPABILITY/v1/...` facade accepts Coucou's fixed local-provider bearer and
does not authorize other API paths and is accepted only on direct loopback,
never through the public HTTPS origin. Failed authentication returns 401;
repeated failures return 429 with Retry-After. Public cookies use Secure.

Providers implement `status()` and `run(task, coordinator, cancel, emit)` in
`hub_providers.py`. Implement a new adapter there and register its config type
in `HubApplication`. Record native IDs and terminal status honestly; never turn
a failed tool or timed-out process into a successful assistant reply.

Hub tools are declared in `hub_runtime.py`: `memory_read`, `memory_put`,
`contexts_list`, `context_switch`, `agent_task`, `task_status`, `notify`, and
`phone_knowledge`. Adding a device capability requires an executor and explicit
ownership/permission handling, not just adding a tool name to a prompt.

The action tools are `capabilities_list`, `capability_call` and
`capability_result`; `agents_list` returns configured provider IDs for delegation.
See the [node protocol](capabilities.md) for schemas, budgets and recovery rules.
`/api/state` includes compact nodes/calls and topic timers. Action creation
returns HTTP 202; inspect its `status`, since built-in actions finish immediately
while remote actions require a node acknowledgement. MCP excludes `agent_task`
and `context_switch`; clients launch owned tasks through the task API.

Command adapters receive one bounded JSON request on stdin and emit JSONL events.
Hermes is the included example. A custom command is trusted owner configuration,
not untrusted text extracted from a chat/webpage. Use synthetic protocol fixtures
and temporary state to test integrations before enabling real side effects.
