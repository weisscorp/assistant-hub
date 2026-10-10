# Connected nodes and actions

Shared memory supplies context. The capability layer supplies actions: a client
asks Hub, Hub locates an advertised function, and its owning node executes it.
The same tools are available to Ollama, Codex and the other model adapters;
Hermes and Agent Orchestrator receive them through Hub MCP. Coucou uses its
existing chat connection to the Hub, with no modified application binary.

## Built-in timers

Ask, for example, **«Поставь таймер на пять минут»**. If duration is missing, the
assistant should ask for it. `hub` advertises `timer.create`, `timer.list` and
`timer.cancel`. The model calls `capabilities_list`, then `capability_call` with
the advertised input schema. The model tool's `arguments_json` is a string
encoding that JSON object; the HTTP/node protocol uses the actual object. This
keeps the model-facing schema portable across APIs that mishandle free-form
object fields. A successful response includes the persisted timer
ID and deadline. No model inference is needed when that deadline arrives.

Timers last from one second to seven days, belong to a topic and survive process
restarts. An overdue timer produces exactly one Hub notification when Hub starts
again. This is a **Hub notification**, not an OS alarm or a phone sound. The UI's
**Узлы и действия** section lists nodes, action outcomes and current-topic timers,
and can create/cancel timers directly. Notifications appear under **Уведомления**.
Cancel a running timer with its exact ID; cancelling a fired timer does not undo
the notification. Notification consumption by a device requires its adapter.

## Connect a computer

The launcher can run its own local worker with two read-only functions,
`system.info` and `system.time`:

```sh
python3 tools/local_stack.py setup --local-node
python3 tools/local_stack.py start
python3 tools/local_stack.py ask 'Узнай через инструменты время на узле local-host'
```

For a separate computer/server, copy its private Hub credential file through
your normal private channel and run the worker there with a unique node ID:

```sh
python3 backend/hub_node.py \
  --url http://10.42.0.1:8778 \
  --token-file /private/path/hub-token \
  --id workstation --label 'Workstation' --kind computer
```

The node registers its catalog and polls outward for commands once a second.
It needs no incoming listener. A successful claim also renews its heartbeat;
after 90 seconds without contact the default worker is displayed as offline.
An offline node can receive a queued command, but execution is not confirmed
until that node acknowledges it. IDs are supplied by the owner, not guessed by
the model. The initial catalog lists up to 100 registered nodes.

## Network transport

The default listener remains `127.0.0.1`. To reach Hub from another VPN member,
set `host` in the private `config.json` to the server's actual VPN interface
address, then restart Hub. The interface must exist on the host. The listener
accepts explicit RFC1918, CGNAT (`100.64.0.0/10`), IPv6 ULA or loopback addresses;
wildcard/public addresses and DNS names are refused. Host/Origin validation
uses that literal address and port. Changing `host` also changes the launcher's
health/browser/Coucou URLs; reconfigure an already-connected Coucou manually.
Update separately configured Hermes/AO `hub_url` values, or rerun their setup.

HTTP must travel inside the owner's trusted encrypted VPN. A private address
alone does not encrypt transport. For the current Android client use its HTTPS
gateway contract; this node worker does not replace the phone's transport.
This feature does not configure VPNs, publish an Internet endpoint or discover
every device automatically. The private Hub token currently trusts its holder
as the owner; per-node permission grants are not implemented.

## Add real application actions

The reference worker accepts an **owner-installed** Python module with
`--handlers /absolute/path/handlers.py`. It exports `CAPABILITIES` and
`handle(name, arguments, context, client)`. The default functions are replaced
by that catalog. Do not load a module from a model response or an imported URL.

```python
from hub_fabric import object_schema

CAPABILITIES = [{
    'name': 'service.status',
    'description': 'Read this node managed service status.',
    'inputSchema': object_schema(),
}]

def handle(name, arguments, context, client):
    if name != 'service.status':
        raise ValueError('Unknown installed action')
    # Replace with the owning service's actual API and verify its response.
    return {'state': 'example'}
```

`context` carries topic, parent task and call IDs. Use
`client.context_packet(context['context_id'], query='specific keywords')` to
retrieve bounded history and indexed relevant knowledge, without fetching the
whole memory store. Results must be JSON (no NaN), at most 12 KB. Handlers must
finish before the 60-second claim deadline; long jobs should expose a start
action returning a job ID and a separate status action.

Schemas use a deliberately small JSON Schema subset: closed `object` properties,
`required`, `string`, `integer`, `boolean`, scalar `enum`, `minimum`, `maximum`,
`maxLength` and `description`. Objects nest at most four levels and contain at
most 16 properties; a node advertises 1–32 actions. A descriptor is at most 6 KB,
a node catalog 48 KB and action input 8 KB. Discovery returns a bounded catalog
(up to eight nodes and about 14 KB); narrow `query` when an entry is truncated.
Model context also receives an up-to-1.5-KB current preview of node IDs and action
names, prioritizing explicitly mentioned nodes. It contains no full schemas and
is not a complete catalog; use discovery for the actual action parameters. Its
space is reserved inside the existing context budget. Historical tool results
must not be mistaken for the current node registry.
Old discovery snapshots remain in SQLite/UI history but are omitted from later
model context. Ollama/cloud message adapters preserve user/assistant roles, with
current metadata supplied immediately before the new request, rather than making
the model reconstruct the dialogue from a JSON log.
If the request explicitly names a node, Hub also publishes up to six ordinary
typed `node_*` model tools, within a 10-KB schema budget. They use that node's
actual advertised schema and the same validation/queue/acknowledgement path.
An optional `_request_key` distinguishes separate identical actions within one
task; retries reuse it. Without it, the typed tool name is the stable key.
The `/api/tools` catalog is bound to a real topic/task; Hermes MCP loads it when
the parent task is supplied. Unnamed nodes still use bounded discovery and the
generic `capability_call` tool.

Tool selection is still model behavior. A small model can omit a call or repeat
an obsolete refusal, particularly in a conversation containing earlier failed
answers. Live verification covers successful Ollama/Hermes calls and a Coucou
timer-then-node-action conversation; it does not establish reliability for every
phrase/history. The broker never executes plain reply text: inspect the actual
call status in the node panel. UI timers and direct API calls do not need a model.

## Confirmation and recovery

| Status | Meaning |
|---|---|
| `queued` | Stored, not claimed by an executor yet |
| `running` | Claimed; awaiting its result |
| `succeeded` | Executor returned an acknowledgement and result |
| `failed` | Executor returned an error, or an unclaimed command expired |
| `cancelled` | Parent cancelled before the command was dispatched |
| `unknown` | Claimed command lost its acknowledgement or Hub restarted |

The model uses a stable `request_key` per action within a task. Hub derives its
UUID from the parent task and that key. Repeating it with the same parameters
returns the same call; changed parameters conflict. API clients supply their
own UUID. Duplicate acknowledgements are idempotent. The worker retries a lost
acknowledgement, **not the handler**. Queued commands expire after five minutes.
Claimed commands become `unknown` after 60 seconds or a Hub restart and are not
replayed automatically. This avoids a blind repeated side effect; it does not
promise exactly-once execution across crashes. Verify unknown outcomes on the
owning node before issuing another action.

`capability_result` reads a call's actual outcome in its topic. Action lifecycle
events and model tool results are persisted. Cancelling an already dispatched
action cannot undo it; the application must advertise its own cancellation
function if it supports one.

## What still needs adapters

The Hub timer and read-only computer worker are implemented. Android remote
execution, Home Assistant/Yandex, Bluetooth controls, app-specific APIs and
system notifications need actual handlers with result checking. Existing mobile
memory/inbox sync is separate from this queue. Registering an action name does
not implement the action. Agents share the broker and context but do not gain
arbitrary access to every application merely by connecting.
