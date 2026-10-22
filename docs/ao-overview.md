# Read-only AO task overview

The mini gateway exposes `ao_overview` to Hermes through its existing bounded
MCP catalog. Like the other tools, it requires an active gateway `request_id`.
It reads all projects' sessions, including orchestrators and terminated sessions;
it does not restrict the overview to workers created by Hub. It does not select,
send, spawn, resume or kill sessions. Existing native task read/select/delivery
flows remain separate. No services are started by this source.

## Source and configuration

`backend/ao_overview.py` uses only `GET /api/v1/sessions` on the mini's AO daemon.
The HTTP read model carries readiness, activity, last event and assistant update
fields that are absent from the CLI list projection. A single read also avoids
the CLI's extra per-session PR summary requests. No AO database schema is needed
for this overview. The older native read/select bridge still uses its existing
local AO database for native conversation ownership and legacy task selection.

Configure `MINI_TASKS_AO_URL` privately in the gateway process environment;
default: `http://127.0.0.1:3001`. A base path is supported. The source rejects URL
credentials, query strings and fragments, disables environment proxies, rejects
redirects, bounds the body to 4 MB and applies a five-second request timeout.
Only connect to a trusted AO endpoint through private network access; this
adapter does not add authentication to AO. Never expose the daemon publicly.
`AOOverview(url=..., timeout=..., stale_after=...)` accepts explicit configuration
for an embedding caller; defaults are five seconds and one hour. Neither
configuration nor responses are written to Git or persisted by the source.

Protocol fixture: `backend/fixtures/ao_sessions.json` is wholly synthetic JSON in
the AO HTTP `{"sessions": [...]}` wire shape, not a capture of an owner's tasks.
The expected AO shape includes `id`, `projectId`, `status`, `statusReadiness`,
`activity.state`, `activity.lastActivityAt` and optional `lastEventAt`,
`latestUserPrompt`, `latestAssistantUpdate`, `harness`, `isTerminated`.
Missing readiness or new status values are conservative unknowns. Missing or
malformed identities, duplicate identities, malformed envelopes, HTTP failures,
truncated/oversized bodies and timeouts make the source explicitly unavailable.
An empty `sessions` array (or AO's null empty slice) is a successful empty list.
There is no pagination or project filter in this endpoint. Oversized lists fail
explicitly instead of silently reporting a partial overview as complete.

## Record and evidence

The snapshot has `source_status` (`available` / `unavailable`), a Russian label,
`checked_at`, `last_success_at`, `is_live` and `tasks`. Times are Unix seconds UTC.
`last_success_at` is null before the first successful validated read and resets
to null after a gateway process restart. Only the exception class is returned;
raw error messages, endpoint addresses and response bodies are not logged.

Each record supplies:

- `expected_result`: null because the AO session list has no structured accepted
  outcome. `task_direction` preserves the latest user prompt when available;
  it is direction, not an inferred acceptance criterion.
- `executor`: stable session ID and the supplied agent harness.
- `last_confirmed_action`: the latest assistant update, if supplied by AO.
  `action_evidence=assistant_report` makes clear that this is a confirmed
  observation of an agent's claim, not independent verification of its content.
  No action time is invented: AO does not expose that update's timestamp here.
- `next_step`: a conservative suggestion with `next_step_kind=suggested`, not an
  invented agent plan. `blocker` and `expected_decision` identify observed waiting
  states; details not present in the source remain explicitly unknown.
- `source`, `ref`, `observed_at`, `raw_status`, `activity_state`,
  `last_progress_at`, `status`, `status_label`, `reason` and `is_live`.

`last_progress_at` is the last AO event time, falling back to activity time;
it is a progress signal, not proof of productive work. `updatedAt` is deliberately
ignored because metadata writes and polling can advance it without progress.
All task direction/update text is untrusted data (`contentKind`), never tool
instructions. This read tool does not authorize any follow-up side effect.

## Status policy

| Status | Russian label | Evidence / limit |
| --- | --- | --- |
| `working` | работает | Ready AO source reports active execution or working status, with no old known event. |
| `waiting_user` | ждёт пользователя | AO `waiting_input` / `needs_input`; wait remains a wait even after a long time. |
| `blocked` | заблокирован | AO activity `blocked`: a pending approval or permission decision; its exact details require inspection in AO. |
| `completed` | завершён | AO reports `merged` delivery on a non-stopped session; this does not independently verify broader task acceptance. |
| `stale` | давно нет прогресса | Active/idle session's last known event is at least `stale_after` old. Silence does not prove a hang. |
| `unknown` | неизвестно | Idle without progress evidence, stopped/terminated/exited session, unready source, or insufficient evidence. |

Termination, agent exit, PR approval and mergeability alone do not establish task
completion. Arbitrary text saying "task completed" does not change the status.
Delivery records (`queued`, `dispatched`, native-command `completed`, `unknown`)
from `task_selected` remain independent: message accepted, instruction delivered
and task completed are different facts. This source does not modify them.

On failure the top-level status is **источник недоступен**. In-memory cached rows
retain their original `observed_at`, have `is_live=false`, `status=unknown` and
`previous_status` for historical context. Suggestions ask to restore and refresh
the source. Cached report/blocker/decision fields are historical too. Consumers
must announce unavailability and `last_success_at` before discussing these rows.
A successful new read replaces the cache completely, including an empty list.
`is_live` means freshly fetched evidence, not a guarantee of a running agent.

## References shared with future sources

Canonical overview key: `source:project/session`, with each opaque ID independently
UTF-8 percent-encoded as one segment (`ao:demo/demo-1`). Source names are fixed
lowercase namespaces. Never use display names, titles, filesystem paths or a
concatenated unescaped pair as identity. AO navigation is separately rendered
as `ao://sessions/{project-id}/{session-id}` using the same segment encoding.
It only navigates inside the AO application; it does not execute an action.
For a projectless AO session the project segment is empty (`ao:/SESSION`),
`source.project_id` and `source.link` are null: no project identity or AO navigation
route is invented. A missing/empty project ID cannot collide with a named project.

A future RCP connector can reuse the source namespace and opaque project/entity
segments, but product, feature, task and AO session are different entities.
Reserve typed references for those entities, for example
`rcp:product/PRODUCT_ID`, `rcp:feature/FEATURE_ID`, `rcp:task/TASK_ID`. Keep explicit
relations such as `implements` / `executed_by` between those references and
`ao:PROJECT/SESSION`; do not assume matching names imply identity. This is only
a reference convention, not an RCP API contract, lookup, storage or migration.
The overview does not access RCP or create a second canonical task database.

## Synthetic verification

```sh
PYTHONPATH=backend python3 -m unittest backend.test_ao_overview backend.test_mini_tasks backend.test_hub_hermes_api -q
python3 -m unittest discover -s backend -p 'test_*.py' -q
```

Tests mock HTTP and use temporary gateway state. They cover all statuses,
project/session identity, orchestrators and terminated sessions, long waits,
misleading delivery/completion text, readiness, configurable staleness, malformed
and oversized responses, timeout/outage with and without cache, recovery and MCP
request grants. They never send to a live task. Optional manual verification is
only a read of `ao session ls --all --include-terminated --json` on an authorized
mini; a sandbox/network denial is not a successful freshness check.
