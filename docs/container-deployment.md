# Hub and extensible collectors in Docker

The existing Hub remains the event receiver, model assessor, history and outbox.
The collector is a separate container: configured read-only sources → local
SQLite outbox → `POST /webhooks/events`. No additional broker is needed for this
single-host deployment. Both images use Python's standard library, run as
`10001:10001`, and support a read-only root filesystem. Models run elsewhere.

## CI images and local verification

Production images are built on GitHub-hosted runners by
`.github/workflows/hub-images.yml`, on pushes to `main`, tags and manual
`workflow_dispatch`. The backend test matrix in `tests.yml` (Python 3.11/3.13)
and the synthetic Compose smoke must pass before publication. Both Dockerfile
targets are published for `linux/amd64` (TrueNAS); a manual dispatch can also
select `arm64`. Dispatch from a feature branch builds without publishing.

Private packages:

- `ghcr.io/weisscorp/assistant-hub`
- `ghcr.io/weisscorp/assistant-hub-collectors`

Tags have the form `<main-or-version>-sha-<12-char-SHA>-run-<run-id>-<attempt>`;
for example `v1.2.0-sha-0123456789ab-run-123456789-1`. Run/attempt suffixes
prevent reruns from replacing an earlier image. There are no moving `latest`,
branch or version aliases. The per-image Actions summary records the tag, full
source SHA and immutable `image@sha256:...` digest. Prefer that digest for
deployment. Keep both image references from the same successful run for rollback.

All third-party actions are pinned by commit. Workflow permissions default to
`contents: read`; only the trusted publication job gets `packages: write` and
uses `GITHUB_TOKEN`. PRs, including forks, run tests, smoke and both amd64 builds
without registry login or publication. No repository secrets are passed to the
reusable tests. An existing non-private GHCR package blocks publication. New
GHCR packages default to private; leave visibility private even though this
source repository is public ([GHCR documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)).

Local development/testing uses Docker Compose on this host. From the repository
root, one command builds both targets and runs the full synthetic smoke:

```sh
python3 tools/verify_hub_containers.py
```

Add `--unit-tests` to run the full backend suite and knowledge benchmark in a
disposable Python 3.13 Compose service first, including synthetic audio test
dependencies. Dependencies are installed inside that disposable container;
the host Python environment and source mounts remain unchanged.

This drives `deploy/hub/compose.test.yaml`: Hub, collectors and a synthetic
Ollama/source server, a random `hub-smoke-...` project name, temporary fixtures
and project-scoped volumes. Only Hub's random loopback port is published. The
runner always calls `docker compose ... down -v --remove-orphans` in `finally`,
including on smoke failure. It never prunes Docker or touches existing projects.
Session-owned containers have an `ao.session` label. Default platform is amd64;
`--platform linux/arm64` is available for native local testing. To test already
built images, pass both `--hub-image IMAGE` and `--collector-image IMAGE`.

The smoke verifies assessment, restart persistence, duplicate suppression,
producer scopes and malformed JSON rejection. It uses disposable credentials
and no user accounts, physical devices, audio, model downloads or paid models.

The root `.dockerignore` allowlists application source. Databases, recordings,
runtime config, credentials, models, repository metadata and tests are excluded.

## Configuration and state

Hub environment:

| Variable | Meaning |
| --- | --- |
| `HUB_OWNER_TOKEN_FILE` or `HUB_OWNER_TOKEN` | Existing owner secret, at least 32 characters; required |
| `HUB_CONFIG_FILE` | Optional mounted JSON config / ConfigMap for provider and integration settings |
| `HUB_OLLAMA_URL`, `HUB_OLLAMA_MODEL` | Default remote Ollama provider when `providers` is absent from JSON |
| `HUB_INCIDENT_TIMEOUT_SECONDS` | Assessment deadline, 15..600 seconds; default 90, use 300 for a slow model cold start |
| `HUB_HTTP_ORIGINS` | Comma-separated explicit HTTP origins for LAN/VPN/Docker hostnames |
| `HUB_PUBLIC_URL`, `HUB_TRUSTED_PROXIES` | Existing HTTPS reverse-proxy contract; configure together |
| `HUB_HOST` | Default `auto`: bind the container's actual private IP; no wildcard listener |
| `HUB_PORT` | Container port, default `8778` |
| `HUB_DATA_DIR` | Persistent volume, default `/data` |
| `HUB_RUNTIME_DIR` | Private temporary config/credential copy, default `/tmp/assistant-hub` |

Mounted config and Secret files may have Kubernetes/Compose ownership. The
entrypoint copies the owner credential into an owned `0600` runtime file before
starting the existing server. The source secret must still be readable by UID
10001. State and recordings stay under `/data`; mount it on local storage.
Use separate volumes per service, one Hub replica, one collector replica, and
Recreate during updates. Do not share live SQLite files over SMB/NFS.

Each HTTP origin is explicit; unknown Host/Origin and untrusted forwarded
headers remain rejected. HTTP aliases are for the owner's private tailnet.
This deployment must not have public ingress, port forwarding or Tailscale Funnel.
The TrueNAS installer publishes port 8778 only on the selected Tailscale IP.

For local Compose, set an owner token in a private env file and run:

```sh
docker compose --env-file /private/hub.env -f deploy/hub/compose.yaml up -d --build hub
```

Create a **webhook-scoped** producer key with `tools/hub_keys.py` inside the Hub
container; save it directly into the collector's private Secret storage. Supply
it through `COLLECTOR_HUB_TOKEN` or `hub.token_file`, then enable the `collectors`
profile. Avoid rendering Compose configs containing literal environment secrets
into shared logs. The collector never needs the owner key.

## TrueNAS

TrueNAS supports custom Compose apps through **Apps → Discover Apps → Install
via YAML**. See the [official installation guide](https://apps.truenas.com/managing-apps/installing-custom-apps/).
`deploy/hub/compose.truenas.yaml` is an example for `/mnt/nvme/assistant-hub`.
Replace both `REPLACE_WITH_CI_TAG` references with exact tags from a successful
Hub images run, or replace each whole image value with its `ghcr.io/...@sha256:...`
reference. The placeholders deliberately cannot install an arbitrary default.
No build or `docker load` is needed on the NAS. Configure the actual Ollama
URL/model; this app does not start a model server. Set the NAS's actual Tailscale
IP and matching HTTP origins. Keep port 8778 bound only to that IP; retain the
private host mounts and UID 10001 permissions.

### Read-only private GHCR access

The owner creates a dedicated GitHub pull account with **Read** access to both
private packages and a PAT **classic** with only `read:packages` (no write/delete
or broad repository scope). Authorize organization SSO if required. In TrueNAS
Apps, use **Configuration → Sign-in to a Docker registry → Add Registry → Other
Registry**: URI `https://ghcr.io`, the pull account username and token as password
([TrueNAS registry guide](https://apps.truenas.com/getting-started/initial-setup/)).
Keep these credentials in TrueNAS credential storage; do not paste them into
Compose YAML, the repository, CI variables, Hub settings or shared logs.
`GITHUB_TOKEN` is only for Actions publication and is not a NAS credential.

For an owner-run diagnostic in the NAS Shell, use `docker login ghcr.io
--username ACCOUNT --password-stdin` with a private token input, then pull the
exact digest. Do not put the token in command arguments or shell history. A
Shell login alone may not configure app-managed pulls; use the registry UI for
the app. If access fails, check account package permissions/SSO/token expiry;
do not make packages public to work around it.

### Manual update and rollback

There is no automatic deployment, image switch, updater, SSH or NAS access in
the workflow. The owner performs these steps in the TrueNAS UI:

1. Choose a fully successful CI run. Save its two exact image digests, current
   app YAML/configuration, previous image digests and the source revision.
2. Wait for Hub tasks to finish, stop this app and take a consistent dataset
   snapshot/backup covering Hub SQLite state and collector outbox. Do not copy
   live SQLite files or reset credentials. Retain this backup and previous images.
3. Edit this app's YAML, changing only the two `image` values to the new explicit
   tags/digests. Keep volumes, secrets, model settings and tailnet binding.
   Apply the edit/start the app; TrueNAS pulls the private images with the stored
   read-only account (`pull_policy: missing`). Do not rerun a first-install script.
4. Over the tailnet, check authenticated Hub health, history, collector status
   and one owner-selected event before accepting the update. Record the running
   image digests and backup reference. CI smoke is synthetic, not NAS verification.
5. To roll back, stop the app and edit both image values back to the saved pair,
   then start and repeat the checks. If a release changed the persisted schema
   incompatibly, restore the matching pre-update snapshot while stopped before
   starting old code. Restoration loses post-snapshot changes; the owner must
   decide that explicitly. Never delete volumes or silently restore a database.

### Existing archive/source updater (legacy)

The following tools describe an older, separately enabled path. They are not
used or enabled by the GHCR workflow above. Prefer CI images and manual app
updates for the current deployment policy; do not build release artifacts on
the NAS or enable the source updater as part of a container update.

`tools/install_hub_truenas.py` automates first installation through the supported
`pool.dataset.create` and `app.create` middleware methods. Run it in the owner's
authenticated root Shell after checking the script and bundle checksum. It:

1. Refuses an existing app or deployment directory.
2. Checks that the selected remote model is already installed.
3. Creates only the named dataset, data/config/plugin directories and private keys.
4. Downloads the owner-built image archive, verifies SHA-256, loads amd64 images.
5. Issues a webhook-only collector key without printing credentials.
6. Registers the two containers as a TrueNAS custom app and waits for its job.

It keeps `installation.json`, `compose.json` and the image archive inside the
dataset. No SSH keys, registry credentials, model downloads, existing application
updates or public listeners are installed. The optional transfer helper serves
only the installer and image archive, only to the specified NAS peer, with a
random path and a finite lifetime. Stop it immediately after transfer.

Existing installations may have the pinned `backend/hub_updates.py`
supervisor and **Hub → Обновления** enabled. It runs inside the existing
unprivileged container, without a Docker socket or TrueNAS administrator token.
The container entrypoint becomes `python3 /data/updater/hub_updates.py`, with
`supervise --root /data/updates --initial RELEASE` as its command and
`HUB_UPDATE_DIR=/data/updates` in its environment. The initial complete release
must already be staged. Keep the original Compose and image for bootstrap
recovery; do not overwrite application credentials or collector configuration.

Build a complete release from a reviewed backend tree:

```sh
python3 tools/hub_release.py build --backend /absolute/reviewed/backend \
  --version hub-20261021-example --output /absolute/hub-release.tar.gz
```

Upload it in the Hub panel. Alternatively, the owner can issue a separate
**Только загрузка релизов · без установки** key under **Доступ**, save it in an
owned 0600 file outside Git, and publish without a browser/NAS login:

```sh
python3 tools/hub_release.py publish --origin https://hub.example.ts.net:8443 \
  --key-file /absolute/private/publisher.key --archive /absolute/hub-release.tar.gz
```

Publication only stages a version. The password owner chooses **Установить** or
**Вернуть эту версию** in Hub. Application, webhook, MCP, publisher and legacy
owner bearer keys cannot activate releases. Publisher keys cannot read chats,
change connections or issue other keys. Revoke them independently in **Доступ**.
Do not expose deployment actions as assistant/MCP tools.

Packages are bounded gzip tar archives with a versioned runtime contract and
file hashes. The receiver verifies the archive SHA-256, paths, types, individual
hashes, complete file inventory and Python syntax without importing uploaded
code. Links, traversal, duplicate normalized paths, unsupported file types and
oversized contents are rejected. Versions are immutable. These checks establish
integrity and compatibility, not that arbitrary code is trustworthy: publish
only reviewed owner-built releases.

Activation stops admission of new Hub tasks and refuses an active task. The
supervisor stops the child, backs up SQLite, starts the selected release and
checks authenticated health with the expected release ID. An unsuccessful
startup restores the databases and previous code. A failed database restore
stops startup for recovery. After a successful switch the previous release is
retained; no history, recordings or application keys are deleted. Browser login
sessions currently require signing in again after a server restart. Expired
session polling stops at the login screen and does not consume password-login
failure limits.

This source-update channel supports the `hub-python313-stdlib-v1` container
contract. Python/dependency/base-image or pinned-supervisor changes require a
separate reviewed container/bootstrap update. For those changes, record image
digests, back up the dataset, pull immutable CI images and use the supported TrueNAS
app update. Never rerun the first-install script over an existing deployment.

## Collector configuration

Use TOML or JSON. `deploy/hub/collectors.example.toml` contains complete settings;
`deploy/hub/configmap.example.yaml` shows mounting the same TOML in a ConfigMap.
Credentials belong in Secret files or environment variables. Restart the
collector after modifying its config or plugin files; there is no hot reload.

```toml
[hub]
url = "http://hub:8778"
token_file = "/run/secrets/collector-token"

[runtime]
state = "/data/collectors"

[[sources]]
id = "jobs"
plugin = "hub_collector_plugins:HttpJson"
interval_seconds = 30
timeout_seconds = 15
mode = "changes"
[sources.options]
url = "https://service.example/api/jobs"
token_env = "JOBS_TOKEN"
pointer = "/data/jobs"
items = true
id_pointer = "/id"
```

Built-ins: `HttpJson` selects JSON with RFC 6901 pointers; `HttpStatus` observes
reachability and status without copying a response body; `Clock` emits events by
wall-clock bucket. HTTP collectors use GET, explicit deadlines, bounded replies,
no environment proxy and no redirects. Source token references are `token_env`
or `token_file`; arbitrary secret headers can use `headers_env = {Name = "ENV"}`.

`changes` compares the kind/data for each source/key. `always` emits each poll.
Observation timestamps and transport IDs are not part of change detection.
For keyed feeds, removed items do not automatically emit deletion events; write
a connector using its cursor if deletions, paging or incremental sync are needed.
Source failures are visible in status and safe Docker log records; they do not
invent health observations for arbitrary plugins. `HttpStatus` explicitly emits
`reachable=false` on connection failure. Calendar/vendor integrations can be
added using the same plugin contract; dedicated adapters are not bundled yet.

## Add a module/class

Mount trusted owner-installed code at `/plugins` or derive a new collector image
with SDK dependencies. Configure `plugin = "my_module:MyCollector"`.

```python
from hub_collector_plugins import Batch, Observation

class MyCollector:
    def __init__(self, options):
        self.options = options

    def collect(self, *, cursor, now):
        # Fetch an incremental page through your service's read-only API.
        records, next_cursor = fetch_page(cursor)
        return Batch([Observation(item, key=str(item['id']), kind='job.changed')
                      for item in records], cursor=next_cursor)
```

`now` is an aware UTC datetime. Cursor/data must be finite JSON; keys must be
unique within a batch. Limit: 256 observations per batch, 60 KB per observation,
32 KB cursor and 1 MB total plugin reply. Plugins run in a separate process with
the configured hard timeout. This is fault containment, not a sandbox: trusted
plugins inherit container permissions and environment. Incoming webhook data
cannot choose a Python module or executable.

The cursor, fingerprints and pending events commit in one SQLite transaction.
A full outbox rolls back that whole batch. Network delivery uses the same UUID
and bytes across retries/restarts; Hub acceptance is not completed assessment or
proof that a user saw a message. Source and delivery claims have bounded leases.
Hub 409/rate limits and transient errors back off; permanent 4xx become `dead`.
An operator can repair credentials/data and retry the retained original event.
Delivered outbox rows expire after seven days by default. Dead rows and
per-source fingerprints remain for inspection/deduplication; monitor disk usage.
Changing a source config resets its cursor/fingerprints, so a fresh snapshot may
be emitted. Payloads retain the original collected data under `data`.

```sh
python3 backend/hub_collectors.py check --config /config/collectors.toml
python3 backend/hub_collectors.py status --config /config/collectors.toml
python3 backend/hub_collectors.py retry-dead --config /config/collectors.toml
```

The existing Hub limits still apply (64 KB webhook, 30 new events/minute/key,
128 incidents awaiting assessment/recipient registration). Pair a notification
client before sustained collection, or operate within that pending limit. Hub
assessment validates a structured model report and preserves raw input; model
confidence does not establish factual validity. The incident assessor produces
summaries/suggestions; autonomous corrective agent execution is a separate
integration and is not silently enabled by this deployment.
