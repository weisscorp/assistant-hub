# Incoming events and notifications

`POST /webhooks/events` accepts any valid JSON value (object, array, string,
number, boolean or null), or UTF-8 `text/plain`. The body is retained as data;
URLs and code inside it are never opened or executed. Limit: 64 KiB per event,
128 incidents awaiting analysis or recipient registration and 30 new events per producer key per minute.

## Authentication and request

Create a separate producer key rather than exposing your owner credential:

```sh
python3 tools/hub_keys.py --config /private/stack/config.json create monitoring \
  --scope webhook --output /private/monitoring.key
```

The key is written once to a private file; the DB stores only its digest. This
scope can only submit webhooks. It cannot read history, incidents or knowledge,
log in to the UI, call tools or run agents. Existing keys retain owner scope.
Revoke it with the same CLI's `revoke KEY_ID` command.

```sh
curl --fail-with-body https://hub.example.com/webhooks/events \
  -H "Authorization: Bearer $(cat /private/monitoring.key)" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 11111111-1111-4111-8111-111111111111' \
  -H 'X-Event-Source: monitoring' \
  -H 'X-Event-Title: Disk%20space' \
  -H 'X-Event-Targets: auto' \
  --data-binary '{"host":"example","free_percent":8}'
```

Use a new UUID for each event and the same UUID for retries. Matching retry
returns the same incident; changed content or producer with that UUID returns
409. Response 202 is acceptance into the queue, not completed analysis or
notification. Metadata headers are optional: source (80 characters), title
(160 characters), targets (`auto`, `phone`, `desktop`, `both`). Percent-encode
non-ASCII source/title as UTF-8. Default source is `webhook`.

Configure Internet exposure separately using [HTTPS proxy access](internet-access.md).
This feature does not change the listening address or publish your running Hub.

## Isolated assessment

Default `incidents.provider` is `ollama`, which must name an Ollama HTTP provider
in the private Hub config. Each incident gets a fresh model conversation with
only its metadata and a preview of up to 12,000 UTF-8 bytes. The full input
remains in SQLite. Assessment is limited to a summary, severity, confidence and
up to three suggestions. It is validated before use.

This is **capability isolation in the application**, not a container or OS
sandbox. The evaluator receives no Hub store, shared memory, previous chats,
tools, shell, network-fetch capability, device keys or native agent runtime.
Hermes and Codex are never launched for these inputs. Recommendations remain
text; they are not automatically executed. Model conclusions are not verified
facts. Failed/unavailable/malformed model responses produce an explicit fallback
notification. A process crash during analysis leaves an `unknown` result for
inspection, without replaying an uncertain operation.

Open the browser UI's **Notifications** tab, or use authenticated owner GET
`/api/incidents` (latest 30) and `/api/incidents/UUID` (including original data).
Raw payloads are displayed as literal text. Each routed report has its own topic;
receiving one never switches your current conversation or adds it to shared
knowledge. Stored incidents are not automatically deleted; include the private
DB in your retention/backup policy.

## Routing and delivery

Clients report recent activity. No GPS, screenshot or microphone is used:

- `auto`: active clients seen in the last 90 seconds with activity in the last
  120 seconds; both types receive it when both are active. With no active client,
  fall back to paired phones, or known desktops if there are no phones.
- `phone` / `desktop`: all registered clients of that type, including offline.
- `both`: wait until at least one phone and one desktop have been registered,
  then deliver to both types.

This approximates where you are using the assistant, not your physical location.
Desktop activity uses OS input idleness and screen lock state; phone activity
means an open assistant screen or an unlocked active assistant conversation.

A durable outbox keeps disconnected clients' messages. Status `submitted` means
accepted by the notification API or written to Coucou's socket, **not read by the
user or confirmed rendered**. Lost desktop receipts/partial writes become
`unknown`; the display action is not replayed automatically. Receipt retries do
not repeat the notification. Android messages have stable IDs and are retained
locally before acknowledgement; blocked notification permission can later be
upgraded to `submitted` after permission is granted.

## Coucou mascot area

Use original [Coucou](https://github.com/Louis-CFM/coucou) on macOS:

```sh
python3 tools/local_stack.py setup --desktop-notifications
python3 tools/local_stack.py start
```

The outbound worker polls the authenticated Hub and sends display-only
`assistant.message` hooks under agent `hub-events` to the configured
`coucou_socket`. It waits while Coucou is closed. The stable device UUID is saved
in private config. Reports appear in the **mascot area**, with no separate native
macOS banner or notification helper. Existing Coucou sound preferences apply.
Coucou 0.2.3 may shorten the visible preview; the complete report remains in Hub.

For another Mac, run `backend/hub_desktop.py --url HUB_URL --token-file KEY_FILE
--id desktop:unique-name --device STABLE_UUID --coucou-socket SOCKET_PATH`.
The display worker receives no executable actions.

## Android

Pair the Android assistant using the same authenticated HTTPS Hub endpoint and
owner key already used for conversation/knowledge sync. No Google services are
needed. Presence, receipts and outgoing events reuse that connection. Older
servers returning 404 for these optional routes still support the existing
knowledge/inbox flow.

Important incident messages use a separate HIGH-importance channel created with
no sound or vibration. The **Hermes and updates → Knowledge and messages** settings
control popups and open Android notification permissions. Ordinary messages and
update notices remain LOW and quiet. Android/user channel settings decide actual
heads-up display; no full-screen takeover is used. See Android's
[notification channels](https://developer.android.com/develop/ui/views/notifications/channels).
No automatic speech, microphone capture, action execution or APK installation is
triggered by an incident. Voice reading remains an explicit user command.

The inbox also has an outgoing event text field and target selector. Voice/text
command: `отправь событие в хаб ...`. Events are saved locally with UUIDs, retried
with the same ID, and limited to 128 queued entries / 32 KB of text per entry.
Background delivery uses the existing direct long-poll/job mechanism. Android
Doze and network loss can delay it; this is not a guaranteed instant push system.
