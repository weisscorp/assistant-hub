# API and extension points

Hub's default origin is `http://127.0.0.1:8778`. Authenticated requests send
`Authorization: Bearer <private-token>`. Browser login uses an HttpOnly,
SameSite cookie, normally obtained through `tools/local_stack.py open`.
Host/Origin checks reject arbitrary origins. This is a single-owner local API.

| Method/path | Role |
|---|---|
| GET `/health` | Authenticated health |
| GET `/api/state?client=NAME` | Providers, topics, client focus and bounded memory |
| POST `/api/contexts` | Create a topic with `title` |
| POST `/api/focus` | `client`, `context_id`, expected `revision` |
| POST `/api/messages` | UUID `id`, `context_id`, `client`, `provider`, `prompt` |
| GET `/api/tasks/UUID` | Durable task state/output/error |
| POST `/api/cancel` | Cancel task `id` |
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
does not authorize other API paths.

Providers implement `status()` and `run(task, coordinator, cancel, emit)` in
`hub_providers.py`. Implement a new adapter there and register its config type
in `HubApplication`. Record native IDs and terminal status honestly; never turn
a failed tool or timed-out process into a successful assistant reply.

Hub tools are declared in `hub_runtime.py`: `memory_read`, `memory_put`,
`contexts_list`, `context_switch`, `agent_task`, `task_status`, `notify`, and
`phone_knowledge`. Adding a device capability requires an executor and explicit
ownership/permission handling, not just adding a tool name to a prompt.

Command adapters receive one bounded JSON request on stdin and emit JSONL events.
Hermes is the included example. A custom command is trusted owner configuration,
not untrusted text extracted from a chat/webpage. Use synthetic protocol fixtures
and temporary state to test integrations before enabling real side effects.
