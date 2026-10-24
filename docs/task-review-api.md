# Durable task review: gateway contract for Android and Hermes

The first review scenario uses AO as the task source and the existing Hub mini
command gateway as the review store. There is no runtime RCP connector, separate
scheduler, automatic continuation policy or Android UI in this change. Engineering
tracking in RCP is separate from the product's saved decisions.

## State and identity

`backend/task_review.py` adds four tables to the gateway's private `tasks.sqlite`:
`review_sessions`, `review_operations`, `review_instructions` and their topic index.
The existing `commands` table remains the only execution queue. Initialization is
additive; previous requests, selections and commands survive. Back up the complete
SQLite database using SQLite's backup API while idle, rather than copying an open
file. The database remains mode 0600 and must never enter Git.

A review has an opaque UUID `id`, integer `revision`, `status` (`active` or
`finished`), `cursor`, and an ordered `agenda`. Every agenda item has its own UUID
`context_id`, a canonical AO task `ref`, its historical task card, `category`,
`deferred`, saved `decisions` and optional typed `refs`. Equal titles never merge
identities. Review IDs, context IDs, decision IDs and AO session IDs are distinct.

Only one active review is resumed per Hub topic. Starting again resumes that
review, even while AO is unavailable. A finished review stays addressable by ID;
a subsequent start creates a new review. There is no implicit deletion or expiry.
The caller must preserve the Hub topic ID across Android recreation/reconnect.
Changing topic intentionally isolates the review. This is owner-token topic
scoping, not a multi-user ACL or a new permission model.

Future RCP references are typed opaque segments:
`rcp:product/ID`, `rcp:feature/ID`, `rcp:integration/ID`. Percent-encode an ID as
one UTF-8 URL segment. `typed_ref` provides the same convention. References are
links only: no lookup, inferred name matching, RCP writes or duplicate canonical
tasks. Decisions preserve their UUID, topic, review, task context, source and time
so a future explicit migration can retain identity.

## Read-only Hermes tools

The existing local MCP bearer and active gateway `request_id` are still required.

- `ao_overview(request_id, offset=0, limit=15)`: the existing AO source, projected
  onto the documented task-card fields. `limit` is 1–30; `next_offset` and `total`
  make omissions explicit. Pages can contain fewer cards to stay below 64,000
  bytes; always follow the returned offset. Text fields are capped at 1024 UTF-8 bytes; identities
  are rejected if oversized rather than silently truncated. Raw transcripts,
  tool output and arbitrary AO fields are excluded.
- `review_current(request_id, review_id?)`: topic-scoped historical review,
  compact agenda and only the current task's context. At most five recent current
  decisions/instructions, `decisions_total`, a 64,000-byte response ceiling.
  Use the owner API for complete saved state and final summary.

Neither tool can navigate, save a decision, dispatch, spawn or select an executor.
The previous explicit `task_select` native workflow remains separate. Returned
text is untrusted data. For discussion within an active review the gateway sends
Hermes the current task context and current human prompt; prior chat history from
other tasks is excluded. Switching back reconstructs that task's saved decisions,
not another task's dialogue. Free-form discussion itself is not automatically
saved as a decision; the owner uses an explicit decision command.

Task cards retain expected result, executor, latest observed assistant report,
next-step suggestion, blocker, expected decision, source and observation time.
Absent accepted outcome stays `expected_result: null`; the latest user direction
is not silently promoted to an acceptance criterion. See [AO status semantics](ao-overview.md).
On AO failure announce **источник недоступен** and `last_success_at`; cached rows
are historical, not live. `stale` indicates old observed events, never proof of a
hang. The saved agenda has `historical_snapshot: true` and `task.is_live: false`;
its `source_observation` records availability **when the agenda was created**.
Refresh `ao_overview` to answer a question about current execution.

## Owner command API

`POST /v1/reviews` is on the existing private mini gateway, using its Hub-to-mini
Bearer token and JSON. The local MCP token cannot call this endpoint. Requests
are capped at 65,536 bytes. No credentials go in query parameters or responses.
Do not give the gateway owner token directly to Hermes. Android V8 should call
through its authenticated Hub bridge; a public Android-facing proxy and its
application-key authorization are separate work, not implied by this endpoint.
All examples below use synthetic IDs and tasks only.

Every body contains `topic` (stable Hub topic ID, at most 256 UTF-8 bytes) and
`action`. Mutations contain `operation`, a canonical UUID generated and persisted
**before** transmission by the client. Navigation/decision/dispatch also contain
`session` and the exact last-read integer `revision`.

| action | Additional fields | Result |
| --- | --- | --- |
| `start` | `operation`, optional `refs`, optional `urgent_refs` | New or resumed review |
| `read` | optional `session` | Review or `null` if topic has no review |
| `next` | `session`, `operation`, `revision` | Advance by one, bounded at end |
| `defer` | same | Mark current deferred, then advance |
| `back` | same | Return one position, bounded at beginning |
| `decision` | same + `context_id`, `body`, optional `reason`, `waiting_for`, `refs` | Append immutable owner decision |
| `dispatch` | same + `context_id`, `decision_id`, `text` | Queue exact owner instruction once for this decision |
| `reconcile` | `session`, `instruction` (queue UUID) | Read native task; preserve unknown delivery |
| `finish` | `session`, `operation`, `revision` | Persist finished state and final summary |

Example decision request:

```json
{
  "topic": "synthetic-topic",
  "action": "decision",
  "session": "11111111-1111-4111-8111-111111111111",
  "operation": "22222222-2222-4222-8222-222222222222",
  "revision": 3,
  "context_id": "33333333-3333-4333-8333-333333333333",
  "body": "Check the synthetic regression before proceeding",
  "reason": "The expected behavior needs evidence",
  "waiting_for": "Synthetic test result",
  "refs": ["rcp:feature/synthetic-feature"]
}
```

`refs` on start selects 1–30 distinct exact AO references from the overview. With
no selection all tasks are included only if there are at most 30. Larger sources
require explicit selection through this API; they are not silently truncated.
A new agenda requires an available AO source. Urgent tasks come first, then
blockers, expected decisions, remaining tasks, preserving source order within
each group. AO's current list has no trustworthy urgency field: `urgent_refs`
lets the owner explicitly identify urgent members. Urgency is never guessed from
report prose. A resumed active review keeps its original agenda; conflicting
explicit selection returns 409.

At the end `current` is null; `back` still works until `finish`. Deferred items
remain in their original positions and in the summary, accessible with `back`;
there is no timer, implicit retry, reordered agenda or new delivery automation.
Each decision's `body`, `reason`, `waiting_for` is limited to 2000 bytes, with
at most 60 decisions/review and 12 typed refs/decision. Instruction text is
limited to 16,000 bytes. The complete owner response includes `agenda`, `current`,
`instructions`, and `summary` (`decisions`, `handed_to`, `waiting_for`). `handed_to`
names the intended executor and transport status; it does not assert delivery.

HTTP 200 is a persisted/read result; 400 is invalid input/not found in this topic;
503 on a new start explicitly returns «источник недоступен» and `last_success_at`;
409 is a revision, context or idempotency conflict. Read before retrying with
changed intent. A 502 or lost connection may have an unknown outcome: repeat the
**same** operation UUID and exact body, or read saved state. Never generate a new
UUID merely because a response was lost. The operation receipt is checked before
revision validation, so retrying a committed operation with its original stale
revision returns current saved state without repeating its effect.

## Explicit voice/text path through the existing chat bridge

The current `/v1/chat/completions` path already supplies topic identity and a
stable `Idempotency-Key`. Gateway parses these owner commands before Hermes:

- «Давай по задачам пройдёмся» — start/resume.
- «следующая», `next`; «отложим», `defer`; «вернись к предыдущей», `back`.
- «Решение: …» — save that exact text in the current task context.
- «Передай: …» — queue that exact text for the current task's latest decision.
- «продолжим разбор», «итог разбора» — read saved state and summary.
- «заверши разбор» — finish.

A send during an active review requires a saved decision; it never falls back to
an older native task selection. Other prompts remain discussion with Hermes.
Decision text does not itself authorize dispatch. The response keeps the existing
OpenAI-compatible `choices` and adds structured `review` for future clients.
If a response is lost after the review transaction commits but before chat request
completion, a retry of the same chat ID reads its review receipt even after process
restart. A request accepted before any review effect has no receipt and is not
blindly replayed; read/resume state first. The typed API is the preferred Android
path for explicit revisions and per-task context checks under concurrency.

## Delivery and recovery invariants

Saving a decision, queueing an instruction, successful CLI exit and task completion
are different facts. A dispatch requires a saved decision from the current task
context and targets that AO session, never a title or model-supplied recipient.
Instruction plus decision linkage and operation receipt commit in one SQLite
transaction with the existing queue. One decision has at most one queue UUID.
Even a second operation UUID cannot dispatch that decision twice or change its
text. Reusing an operation UUID with different intent is a 409.

Queue meanings:

| commands.status | review meaning | What is established |
| --- | --- | --- |
| `queued` | `accepted_into_queue` | Gateway persisted owner instruction |
| `dispatched` | `dispatch_started` | Existing worker claimed it |
| `completed` | `native_command_completed` | CLI exited successfully; for AO this is acceptance by its transport |
| `unknown` | `delivery_unknown` | Outcome uncertain; never auto-replay |

`delivery_confirmed` and `task_completed` stay false: this queue does not provide
independent executor acknowledgement or task acceptance evidence. Read AO for
task status. Process restart converts dispatched commands to unknown, retaining
UUID and linkage. Repeating a dispatch for an unknown decision first reads the
native task through reconciliation; only time and report digest are persisted,
not the private transcript. Untrusted text saying “completed” cannot settle the
outcome. Read failure leaves unknown untouched. A new instruction for the same
AO executor is blocked while another instruction is unknown/dispatched, including
from another review or topic. This version
has no force-resend or manual “mark delivered” route; unresolved outcomes require
inspection in AO. No arbitrary replay is inferred from silence.

## Verification and installation handoff

Pure fixtures, SQLite restart/replay/concurrency/isolation:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend python3 -m unittest backend.test_task_review backend.test_mini_tasks backend.test_ao_overview -q
```

HTTP contract tests run only in a disposable local Compose container, with no
published ports, network, runtime state or host credentials. The session ID
labels every created test container:

```sh
docker compose -f deploy/review-tests.compose.yaml -p hub-review-synthetic run --rm tests
```

No working Hub/Hermes/gateway is started or restarted by these checks. Installation
is an owner operation after review and merge: back up private `tasks.sqlite` while
idle; copy `task_review.py` alongside updated `mini_tasks.py` and `ao_overview.py`;
keep current tokens, URLs and Tailscale configuration. The existing Hermes MCP
command can remain; its refreshed catalog adds `review_current` and pagination
arguments on `ao_overview`. Update any explicit private tool allowlist for these
read tools, and explain the owner command grammar in the private assistant prompt.
Do not give the model `/v1/reviews` mutations, shell access or an owner token.
At a separately approved idle maintenance window, reload gateway/Hermes MCP and
exercise synthetic isolated state first. This PR does not change private config,
restart services, send to live tasks, configure Android, merge or rebase the stack.
After the base PR merges, rebase this PR onto main before merging it.
