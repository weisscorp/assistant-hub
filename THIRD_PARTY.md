# Third-party projects and attribution

The MIT license at this repository's root covers our Hub/coordinator, adapters,
UI and documentation. It does not replace any upstream license or grant rights
to models, hosted service data, branding or binaries installed separately.

| Project / authors | Used for | Upstream terms | Distribution here |
|---|---|---|---|
| [Coucou — Louis Raillé](https://github.com/Louis-CFM/coucou) | Original desktop mascot/chat; model facade and socket events | [MIT](licenses/Coucou-MIT.txt) | Integration only; no application binary or mascot assets |
| [Hermes — Nous Research and contributors](https://github.com/NousResearch/hermes-agent) | Official AIAgent runtime and tool registry through Hub MCP | [MIT](licenses/Hermes-MIT.txt) | Adapter only, runtime installed separately |
| [Agent Orchestrator — OrchestratorInc and contributors](https://github.com/OrchestratorInc/agent-orchestrator) | Owned multi-agent worker sessions | [Apache-2.0](licenses/Agent-Orchestrator-Apache-2.0.txt) | Adapter only, daemon installed separately |
| [OpenMausBot — Milind Soni and contributors](https://github.com/milind-soni/OpenMausBot) | Separate coding bot, durable guarded chats and completion tracking | [Apache-2.0 core](https://github.com/milind-soni/OpenMausBot/blob/8b53c0323dba2099a953ee2d77ab9330cd5acaa7/LICENSE); [enterprise has separate terms](https://github.com/milind-soni/OpenMausBot/blob/8b53c0323dba2099a953ee2d77ab9330cd5acaa7/enterprise/LICENSE) | Independently implemented HTTP adapter only; no upstream binaries, UI assets or enterprise code |
| [Codex — OpenAI and contributors](https://github.com/openai/codex) | Native app-server protocol and authenticated CLI workers | [Apache-2.0](licenses/Apache-2.0.txt) | Adapter only, CLI installed separately; desktop app not bundled |
| [Ollama and contributors](https://github.com/ollama/ollama) | Local model runtime/tool API | [MIT](https://github.com/ollama/ollama/blob/main/LICENSE) | HTTP integration; no runtime/weights |
| [MCP Python SDK — Anthropic and contributors](https://github.com/modelcontextprotocol/python-sdk) | Standard stdio tool transport and HTTP interoperability checks | [MIT](licenses/MCP-Python-MIT.txt) | Pinned optional dependency, installed separately |
| [SQLite](https://www.sqlite.org/) | WAL storage and FTS5 indexed recall | [Public domain](https://www.sqlite.org/copyright.html) | Used through Python's standard library |
| [Python](https://www.python.org/) | Runtime/standard library | [PSF terms](https://docs.python.org/3/license.html) | Installed separately |

Development integration pins: Coucou v0.2.3; Hermes
`bb2362878b33e14e8b7d24d621510e087269fdce`; Agent Orchestrator v0.13.5;
MCP Python SDK 1.26.0. License reference copies above preserve upstream copyright
text and identify the integrations; they do not mean those applications are
rebundled or relicensed. Verify compatibility and notices when upgrading.

OpenMausBot integration pin: npm `openmausbot@0.1.107`, HTTP contract checked
against commit `8b53c0323dba2099a953ee2d77ab9330cd5acaa7`. Its coding CLIs,
model subscriptions and optional integrations retain their own terms.

OpenAI/Anthropic/OpenAI-compatible service adapters are independently implemented
protocol clients. Provider names remain their owners' names. Configure credentials
and comply with the chosen service's terms. Model licenses are independent of
both the Hub's license and its protocol compatibility.

The OpenAI dot adapter independently implements the documented
[MCP Events contract](https://developers.openai.com/plugins/build/mcp-events).
ChatGPT/dots are hosted services, not bundled model weights or software licensed
by this repository. The optional OpenAI Secure MCP Tunnel client is installed
separately; no tunnel binary or account credential is distributed here.

The Android companion has a separate [dependency/voice notice](https://github.com/weisscorp/android-assistant/blob/main/THIRD_PARTY.md).
No claim is made that an APK built with arbitrary third-party voices is MIT-only.

Desktop audio transcription optionally uses separately installed
[SpeexDSP](https://gitlab.xiph.org/xiph/speexdsp) 1.2.1 acoustic echo cancellation
through its public C API ([BSD-style license](https://gitlab.xiph.org/xiph/speexdsp/-/blob/SpeexDSP-1.2.1/COPYING)).
No SpeexDSP source or binary is bundled. Upstream authorship and notices remain
with the system package. Raw captures are not filtered or replaced.

Speech cleanup uses the separately installed FFmpeg `arnndn` filter and a pinned
[Gregor Richards RNNoise model](https://github.com/GregorR/rnnoise-models),
`somnolent-hogwash-2018-09-01/sh.rnnn` at commit
`3eee541a283fd3b8f81b85b1748e3b9ccbefa04d` (SHA256
`70bb6685eb0c2a1d18e2918dca3fbfbd39317010b1802eb1b6ea73a92f3fdec0`).
The upstream README declares the model data non-copyrightable; its tools and
README are excluded from that statement. No model, upstream code or FFmpeg
binary is bundled here. Installed FFmpeg retains its own license/build terms.
