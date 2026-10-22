# Hermes and task access on the owner's Mac mini

Hub remains on the NAS. Its Hermes command provider can call the mini's
`mini_tasks.py` gateway over Tailscale HTTPS. The gateway calls Hermes on
loopback; Hermes uses the official experimental
[Claude subscription provider](https://github.com/NousResearch/hermes-official-plugins/tree/main/claude-subscription-directsdk).
Claude Code owns its login. Never copy OAuth credentials to Hub or NAS.

Runtime files belong outside Git in `~/.local/share/assistant-control`:

- `backend/`: mini_tasks.py, codex_context_bridge.py, agent_hub.py, knowledge_search.py.
- `state/token`: random Hub-to-mini bearer, mode 0600.
- `state/mcp-token`: a different random local MCP bearer, mode 0600.
- `state/tasks.sqlite`: prompts, topic selections and durable delivery state, mode 0600.

Directories must be private. The Python service listens on 127.0.0.1:8643;
Hermes listens on 127.0.0.1:8642 with a separate API_SERVER_KEY. Tailscale Serve
can publish 8643 on a tailnet-only HTTPS port. Do not use Funnel, wildcard
listeners or public ingress. Keep the existing Serve configuration intact.
Use launchd for both services. Do not restart the task gateway while native
work is dispatched: inspect its command table first.

Hermes configuration:

```yaml
model:
  provider: claude-subscription-directsdk-experimental
  default: sonnet
platforms:
  api_server:
    enabled: true
    extra:
      host: 127.0.0.1
      port: 8642
platform_toolsets:
  api_server: [work_tasks]
mcp_servers:
  work_tasks:
    command: /absolute/path/to/python3
    args: [/absolute/path/to/mini_tasks.py, mcp, --state, /absolute/private/state]
```

Only four MCP tools are exposed: list, read, select, selected/delivery status.
No shell, arbitrary file paths, credential tools or send tool is exposed to the
model. Each call must reference a live request registered by the gateway.
The existing hub_hermes_api bridge already supplies the final human prompt,
topic packet and stable request UUID. No Hub payload change is necessary.

Example requests in the shared Hub topic:

- «Покажи последние задачи Codex с синим кружочком». This uses native read receipts
  without marking anything read. Other engines currently have unknown unread state.
- «Прочитай результат задачи …» or «Что она сейчас делает?». Stored turns and
  AO's observed status are distinguished from live process status. An independent
  Codex app-server does not know the GUI runtime's active state.
- «Выбери задачу …». Selection is saved for this Hub topic.
- «Передай: проверь тесты и расскажи, что осталось». Only this explicit prefix
  sends the exact human text to the selected native session. Discussion of a
  report cannot itself authorize a send.
- «Какой статус у переданного поручения?».

AO sessions are sent through `ao send` with a stable client-message-id and
`--steer`; underlying Codex/Claude conversations are excluded from the native
lists to avoid offering two write paths to the same managed task. Standalone
Codex and Claude sessions use their official resume clients. No approval,
sandbox, hook trust or model overrides are supplied. Interactive approvals may
require the native client; this gateway does not grant them.

Busy Codex turns wait in the durable queue. Native command processes remain
alive until they finish. Dispatched commands are never replayed after failure
or service restart: their state becomes `unknown` and requires inspection.
A queued acknowledgement is not proof of delivery or task completion.
Claude transcripts are bounded and have no reliable cross-client live/unread
status. Very old sessions outside the bounded recent list are not selected.

Before changing the NAS provider URL, back up its configuration and key, verify
the mini HTTPS endpoint from the NAS, and keep the NAS Hermes available until
the new route passes an actual Hub request. Use distinct client/application
keys; do not replace Coucou or phone keys for this backend migration.

## Управление из Hub

Раздел **Подключения** доступен владельцу после входа по логину и паролю.
Можно добавить несколько VPN-серверов Hermes, проверить доступ и отдельно
выбрать активный. Настройки переживают перезапуск; консоль TrueNAS для смены
адреса, ключа или сервера больше не нужна после установки этой версии Hub.

Проверка читает `/v1/models`, без генерации и расходования лимита модели.
Сохранение нового сервера не включает его автоматически. Переключение
повторно проверяет адрес и ключ; при ошибке прежний сервер остаётся выбранным.
Текущие вопросы используют снимок адреса и ключа, а новые — новые настройки.
Исходный командный провайдер сохраняется как вариант возврата.

Ключи соединений хранятся в приватном каталоге состояния Hub
`hermes-connections/`, отдельными неизменяемыми файлами 0600. Парольные
сессии владельца могут читать метаданные и изменять подключения; API-ключи
приложений, старые owner-ключи, webhook и MCP не могут. Ни GET, ни ответы
сохранения не возвращают ключи. При смене URL требуется новый ключ — старый
не отправляется на другой сервер автоматически. Настройки имеют ревизии
для защиты от одновременных правок из разных вкладок.

API панели: GET `/api/connections`, POST `/api/connections/test`,
`/api/connections/save`, `/api/connections/activate`. Проверка/сохранение принимают
`revision`, `id` (для правки), `name`, `url`, `model`, `token`. Пустой token при
прежнем URL сохраняет существующий ключ. Активация принимает `revision`, `id`;
`id: null` возвращает исходный командный адаптер, если он был настроен.

Synthetic checks:

```sh
PYTHONPATH=backend python3 -m unittest backend.test_mini_tasks backend.test_hub_hermes_api -q
```

Do not test sends in a user's existing task. Use fake native adapters; live
verification reads only. Do not start a new production conversation merely as
a smoke test. Keep task text, credentials and auth callback codes out of logs.
