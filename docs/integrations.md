# Connect the local stack

Start with the [core quick start](../README.md). All runtime files below are in
`~/.local/share/assistant-stack` (0700), not in either public repository. Restart
Hub after changing its private configuration. Configure providers you actually
intend to use; the launcher does not install upstream applications for you.

## Ollama

Install/start Ollama from its [official project](https://github.com/ollama/ollama).
The generated config selects `http://127.0.0.1:11434` and
`qwen3:4b-instruct-2507-q4_K_M`. Pull that model, or replace both fields with your
own locally available instruct model. A running HTTP service alone does not
mean its model supports the tool calls required for memory operations.

## Codex native authentication

Install the [official Codex CLI](https://github.com/openai/codex), then sign in
through its supported flow:

```sh
codex login
codex login status
python3 tools/local_stack.py setup
python3 tools/local_stack.py stop
python3 tools/local_stack.py start
python3 tools/local_stack.py ask --provider codex 'Какие знания тебе доступны?'
```

`setup` detects `codex` on PATH and creates an isolated agent workspace. The Hub
starts a native stdio app-server and uses dynamic Hub tools; authentication stays
in Codex's own credential store. There is no token export into a provider API key.
The adapter runs new owned sessions with a read-only sandbox and rejects unrelated
shell/approval requests. It does not resume or send messages into existing desktop chats.
The app-server protocol evolves: use a compatible CLI and retest after upgrading.
[Official authentication](https://developers.openai.com/codex/auth) and
[app-server protocol](https://developers.openai.com/codex/app-server) are the references.

To explicitly list/import a bounded context from accessible native chats, inspect
`python3 backend/codex_context_bridge.py --help`. This separate bridge only calls
read methods; importing does not authorize continuing someone else's chat.

## Hermes

Install official [Nous Research Hermes](https://github.com/NousResearch/hermes-agent)
in its own checkout/environment using its upstream installation instructions.
The development integration was exercised at commit
`bb2362878b33e14e8b7d24d621510e087269fdce`. It uses internal AIAgent/registry APIs,
so other versions require compatibility checking.

Create the Hub MCP environment and configure the actual Hermes paths:

```sh
python3 -m venv verification/home-mcp-venv
verification/home-mcp-venv/bin/python -m pip install -r backend/requirements-mcp.txt
python3 tools/local_stack.py setup \
  --hermes-root /absolute/path/to/hermes-agent \
  --hermes-python /absolute/path/to/hermes-environment/bin/python
python3 tools/local_stack.py stop
python3 tools/local_stack.py start
python3 tools/local_stack.py ask --provider hermes 'Прочитай доступные знания'
```

The launcher pins the current Git revision, creates a separate `HERMES_HOME`,
uses the configured local Ollama endpoint, and disables progressive tool search
only in this isolated profile. The adapter exposes the bounded Hub MCP catalog directly and
checks the exact tool set. Terminal/file/device toolsets are not enabled by this
adapter. An upstream change requires revalidation and a new `setup`.

This catalog includes shared node discovery, action calls and call results;
Hermes can use the same timer and registered device capabilities as other
providers. MCP does not enable arbitrary shell/device tools. Connect real
executors using the [node protocol](capabilities.md).

For another trusted MCP client, run the stdio server with its assigned topic:

```sh
verification/home-mcp-venv/bin/python backend/hub_mcp.py \
  --url http://127.0.0.1:8778 \
  --token-file ~/.local/share/assistant-stack/token \
  --context TOPIC_UUID --client YOUR_CLIENT
```

## Coucou on macOS

Install the original [Coucou](https://github.com/Louis-CFM/coucou) application
(v0.2.3 was used during development). Its mascot and animation remain upstream
work. No replacement binary or mascot assets are distributed in this project.

Generate your private local connection address:

```sh
python3 tools/local_stack.py coucou-url
```

In Coucou Settings → Chat → Local models → Ollama, enter that address and connect.
Choose `assistant-ollama`, `assistant-codex`, `assistant-hermes` or
`assistant-orchestrator` when the corresponding provider is configured. The
address contains a private scoped capability; do not publish it in screenshots,
issues or configuration examples. It grants only the model/chat facade, not
administrative Hub endpoints.

The default socket is `~/Library/Application Support/NotchBuddy/nb.sock`. Hub emits
lifecycle/tool/response events there. Coucou's normal Hermes hook plugin is a
separate integration with a standard Hermes profile; the Hub's isolated Hermes
adapter already emits Hub events and does not need that plugin. Hook/approval
compatibility depends on upstream versions. The current Hub does not implement
a phone version of Coucou.

The launcher can optionally own a Coucou installation specified as `coucou_app`
in private config, or found at `runtime/coucou/Coucou.app` in the runtime directory.
That optional path configures its existing local provider and launches through
macOS; the normal manual connection above also works with `/Applications/Coucou.app`.

## Agent Orchestrator

Install an appropriate official [v0.13.5 release](https://github.com/OrchestratorInc/agent-orchestrator/releases/tag/v0.13.5).
Put its `ao` binary at `~/.local/share/assistant-stack/runtime/ao`, make it executable,
and create the MCP environment above. Run `setup` again, then stop/start.
Launcher owns a separate daemon/data directory on `127.0.0.1:3001`. Its provider
starts owned Codex worker sessions, gives them a scoped Hub MCP helper and tracks
session state. It does not attach to all pre-existing AO sessions automatically.

## Optional cloud providers

Generated `openai` / `anthropic` entries have an empty model and stay disabled.
Choose a model available to your account in private config and provide
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` in the environment of the new Hub process.
Use your normal secret manager or private environment; never commit keys.
A cloud entry also supports a private `key_file` instead of `key_env`.

A custom endpoint uses `type: cloud`, `protocol: openai-compatible`, a URL and model.
Protocol fixtures cover tool exchanges; real paid API behavior has not been live
validated. Cloud use sends the selected conversation/history/facts to that provider.
Remote chat delegation is explicit; a missing local fact is not automatic consent
to send the entire database to a cloud model.
