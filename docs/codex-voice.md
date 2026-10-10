# Codex completion events and voice

Hub observes native Codex turns and publishes terminal reports through the same
isolated incident/outbox pipeline as `POST /webhooks/events`. Read-only discovery
is separate from the writer used for an explicit continuation.

## Enable on the Codex host

Install/sign in to the official Codex CLI using its native authentication. No
OpenAI API key or token export is required.

```sh
python3 tools/local_stack.py setup --codex-link
python3 tools/local_stack.py stop
python3 tools/local_stack.py start
```

The private stack config gains:

```json
{
  "codex_link": {
    "binary": "/absolute/path/to/codex",
    "host": "local",
    "poll_seconds": 15,
    "targets": "auto"
  }
}
```

Optional `socket` is an absolute Unix socket for an existing compatible Codex
app-server; the bridge connects using `codex app-server proxy --sock PATH`.
Without it, the bridge runs its own stdio app-server and reads stored native
sessions. It does not assume it can see live activity in a different process.
Permission policy, model and workspace of an explicitly resumed thread stay
with that native session. A writer-lock conflict fails closed; the bridge does
not take over another client's active turn or interrupt it.

Polling reads the indexed recent-thread catalog (at most 1,000 records). It
fetches full turn items only when changed terminal results need their reports.
Pages of five turns overlap the last observed turn, covering up to 100 new turns
per changed thread; unchanged completed threads reuse the cache. Empty threads are not
re-read on every tick. First discovery makes old reports readable without
sending historical alerts. New terminal turns are keyed by host/thread/turn/status
and enter a durable, deduplicated outbox; pending events survive disconnection.

`completed`, `failed` and `interrupted` are distinct. A completed turn means its
response ended; it does not prove an entire multi-turn goal, deployment or test
suite succeeded. The saved report contains final answer text (up to 12 KB UTF-8),
error and up to 20 changed file names. Commentary isn't the final report.
An unavailable observer is disclosed as a stale saved status.

## Voice or text

Android 0.35.0 sends these commands straight to the authenticated Hub, even in
local-model mode. Coucou and the Hub chat recognize the same commands before
calling a model.

1. **«Задачи кодекса»** — recent tasks and saved states.
2. **«Выбери задачу кодекса Название проекта»** — select by title; ambiguity asks
   for a more specific title. The owner API also accepts Hub chat UUIDs.
3. **«Статус кодекса»** or **«Прочитай отчёт кодекса»**.
4. **«Передай кодексу Добавь проверку обработки ошибки»** — queue this exact text
   in the selected native session. Acceptance isn't execution confirmation.

Selection belongs to the requesting client and current Hub topic. It cannot
silently carry across another topic/device. A busy native turn isn't steered or
interrupted by this command. Native approval requests aren't approved by Hub.
Resolve them in the owning Codex client where supported, or use that client to
continue; an approval that this bridge cannot handle may fail the turn.

The continuation is persisted before `turn/start`, with a stable request UUID.
No uncertain mutation is retried. If Hub restarts or the native connection is
lost, the command becomes unknown until a matching terminal turn is observed.
A lost response before obtaining a native turn ID requires manual inspection in
Codex. The owner can then `POST /api/codex/acknowledge` with
`{"id":"command UUID","client":"requesting client","context_id":"topic UUID"}` to
release that guard; this acknowledges uncertainty and **does not retry** or mark
the native task successful.

Hub's “Agents and tasks” page also offers “Report” and “Select for conversation”
on native Codex chat cards. Full saved reports remain readable in a scrollable
dialog without being truncated to the short voice answer.

## Delivery and remote producers

Reports are assessed as isolated data by the configured incident model, with no
tools, shared memory or permission to launch Codex. The original report remains
in the incident payload; voice reads the actual saved native final answer rather
than an inferred model summary. Phone/Coucou delivery uses existing presence,
target selection, stable IDs and submission receipts. Native phone notifications
are quiet; voice reading is explicit. No microphone or app is launched remotely.
Doze, offline phones and the evaluator can delay delivery.

For a Codex runner on another host, forward a terminal app-server turn (or its
`turn/completed` envelope) through the existing authenticated webhook:

```sh
python3 tools/codex_webhook.py \
  --url https://your-hub.example \
  --token-file /private/codex-webhook.key \
  --host workstation \
  --thread-id native-thread-id \
  --title 'Example project' \
  --targets auto < terminal-turn.json
```

Create a **webhook-scoped** key using `tools/hub_keys.py`. It can publish events
but cannot read reports, select sessions or send messages. Identical payload
retries reuse the same UUID; changed content with that UUID returns conflict.
This CLI doesn't install or overwrite Codex hooks. Connect it to the terminal
event of your runner; it refuses in-progress turns. Remote webhook reports are
data-only incidents, not remotely controllable sessions: the native voice bridge
controls only its configured host.

## Owner API and model tools

- `GET /api/codex/status`, `/api/codex/chats?query=...`
- `GET /api/codex/chats/{Hub chat UUID}` — full saved report and observation time.
- `POST /api/codex/voice`: `id`, `client`, `text`, optional `context_id`.
- `GET /api/codex/commands?client=...&context=...`
- `POST /api/codex/acknowledge` — manual uncertainty acknowledgement.
- Model tools `codex_chats` and `codex_report` are read-only. A model cannot
  authorize arbitrary continuation by putting instructions in a report.

Protocol checked against native CLI `0.162.0-alpha.2`. Recheck compatibility when
upgrading: [official app-server documentation](https://developers.openai.com/codex/app-server).
Tests use synthetic sessions and temporary databases; real personal chats are
never sent test messages.

An unreadable/unsupported old rollout is quarantined for five minutes instead of
blocking other threads. Its previous report is retained; stale/reading-unavailable
states are visible to the voice client. If an old thread returns to the catalog
after falling outside the recent window, only its observed latest result can be
reported; the monitor isn't a lossless audit stream of every historical turn.
