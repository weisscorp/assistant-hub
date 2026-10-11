# Development OTA service

`backend/dev_ota.py` adds an opt-in development channel alongside Hub. It serves
authenticated HTTPS `GET /updates/manifest` and `GET /updates/<artifact>.apk`.
Use a separate random OTA bearer key; it grants no conversation, webhook or agent
access. Bind an explicit private LAN/VPN IPv4 address. No public port, ADB, phone
connection, microphone, or sound is enabled by installing this repository.

## Provision on a development Mac

Prepare and review the Android repository's native dependencies and offline voice
assets first. The setup command copies those ignored inputs outside the editable
checkout; their dependency lock must match every subsequent build. It reuses an
existing matching Android **debug signing key**, solely for this private channel.
It does not create a replacement identity for an already installed app.

```sh
python3 tools/dev_ota_setup.py \
  --state "$HOME/.local/share/assistant-dev-ota" \
  --checkout /absolute/path/to/android-assistant \
  --host 192.168.1.2 --port 8779 \
  --sdk /absolute/path/to/Android/sdk \
  --java-home /absolute/path/to/jdk17 \
  --build-tools /absolute/path/to/Android/sdk/build-tools/35.0.0 \
  --debug-key "$HOME/.android/debug.keystore" \
  --minimum-version 80 --launch-agents
```

Only `--launch-agents` starts persistent macOS services. They run as the owner,
restart on login/failure, and write private logs. The state directory is mode 0700;
keys, configuration, credentials and phone configuration are mode 0600. The
self-signed certificate includes the selected IP SAN; transfer its printed SHA-256
fingerprint through a trusted channel. A new IP/certificate must be provisioned
explicitly and the phone's saved pin updated. The phone rejects expiry and SAN
mismatches. The setup refuses incomplete identities instead of rotating them.

Without LaunchAgents, run these in separate terminals:

```sh
python3 backend/dev_ota.py serve --config /private/path/config.json
python3 backend/dev_ota.py watch --config /private/path/config.json
```

Import private `phone-setup.json` in Android Settings → Server → Dev OTA, then
delete the external copy. Grant Android's one-time install-apps access. APKs are
checked for package, version, SHA-256, alignment and signing identity. Android
tries silent self-updates; it may require a quiet user confirmation. Background
checks require an unmetered network and can be delayed by Doze. The app pauses
installation during conversations, foreground use, calls and ringing alarms.

## Build and publication contract

The worker polls the **local `main` branch** every 30 seconds. A source checkout
must be clean and HEAD must equal that selected branch. Commit approved changes
to trigger a build. It does not fetch untrusted PRs or publish uncommitted work.
The worker checks out the exact commit in a separate build clone, restores only
provisioned ignored native/voice inputs and runs assembleRelease, unit tests and
lintRelease. It preserves build caches between commits. Tracked assets/catalog
are taken from the commit, never overwritten by a frozen input copy.

Signing credentials are stripped from Gradle's environment and supplied only to
the signer after checks pass. This owner-run process is **not a security sandbox
for hostile Gradle code**; use the separate-UID `release_service.py` deployment
for a stronger production boundary. Do not submit arbitrary third-party source.
Neither this service nor Hub accepts a remote command to modify the build recipe.

Versions are reserved durably, increase monotonically and survive worker crashes.
Failed checks leave the previous manifest untouched and retry with backoff.
Successful commits are not built repeatedly. Signed APKs are immutable, and
promotion atomically swaps only the manifest. Previous artifacts remain available
so a concurrent publish does not break an in-flight download. Integrity is cached
by file identity, so polling does not rehash hundreds of MB each time. The initial
implementation retains artifacts; monitor disk usage and remove obsolete releases
only after clients have had time to finish their downloads.

Inspect `worker/build-state.json`, `worker/build.log`, `watch.log`, and `serve.log`
in the private state directory. To stop on macOS:

```sh
launchctl bootout "gui/$(id -u)/local.assistant.dev-ota.watch"
launchctl bootout "gui/$(id -u)/local.assistant.dev-ota.serve"
```

For Linux/TrueNAS, use the same private config and scripts with a service manager
and installed Android/JDK toolchain. HAProxy can terminate trusted HTTPS in front
of a separate artifact service; this LAN setup does not change the existing Hub
proxy or publish it to the Internet. Dedicated server tests use temporary Git
repositories, fake APK bytes and synthetic keys.

## Kubernetes development service and Mac build worker

`deploy/dev-ota/Dockerfile` packages only the artifact service and validation
tools. Build it from the repository root for `linux/amd64`. The image runs as
UID/GID 10001 with Python 3.13, JRE 17 and official Android build-tools 35. The
tools ZIP is checked against its official 61,958,799-byte size and SHA-256
`bd3a4966912eb8b30ed0d00b0cda6b6543b949d5ffe00bea54c04c81e1561d88`.
The existing Mac worker continues to build/test/lint/sign approved clean Android
commits. The cluster receives signed APKs and the public signing-certificate
fingerprint; it receives no keystore, signing password or editable Android code.

Configure the container through these environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `POD_IP` | required | Explicit private IPv4 bind address from the downward API |
| `OTA_PORT` | `8443` | HTTPS listener |
| `OTA_OUTPUT` | `/data/releases` | Persistent immutable artifacts and atomic manifest |
| `OTA_SECRET_DIR` | `/var/run/ota-secrets` | Read-only projected Secret |
| `OTA_PRIVATE_DIR` | `/run/ota/identity` | Service-owned child of a writable emptyDir |
| `OTA_BUILD_TOOLS` | `/opt/android-build-tools` | Fixed server-side validator tools |

The Secret contains exactly the required identity files `ota.key`,
`publisher.key`, `tls.pem`, `tls.key`, and `certificate.sha256`. Use two different
random bearer keys: the phone's `ota.key` permits downloads; `publisher.key`
permits manifest reads and publication. Secret projections can be mode 0440
with `fsGroup: 10001`. At startup the service copies only these files into its
owned mode-0700 directory, with each private file mode 0600. A root-owned
emptyDir mount itself is unsuitable as `OTA_PRIVATE_DIR`; use the child path
above. Provide writable emptyDirs at `/run/ota` and `/tmp`, plus an fsGroup-writable
PVC at `/data`. The service supports a read-only root filesystem. Android-tool
temporary files and incoming upload snapshots need space as well as retained
APKs: budget at least two APK sizes during validation. The current deployment
retains previous releases; monitor PVC capacity.

TLS remains terminated in Python. The certificate SAN must include the LAN/VPN
address exposed by the Service, such as `192.168.1.20`; a NodePort or TCP proxy
forwards TLS unchanged. The service refuses a wildcard bind or public bind IP.
`GET /healthz` is an unauthenticated liveness probe returning only
`{"status":"ok"}`; it works before the first release is published. A healthy
listener does not imply that a release is available.

Add an optional block to the Mac worker's existing mode-0600 configuration:

```json
{
  "remote_publication": {
    "endpoint": "https://192.168.1.20:30779",
    "token_file": "/private/path/publisher.key",
    "tls_cert": "/private/path/trusted-ota-certificate.pem"
  }
}
```

The publisher key must be an owned mode-0600 file. The client verifies the
server certificate and hostname against `tls_cert`; there is no insecure TLS
mode. Provision the certificate through a trusted channel. Changing the server
address or certificate requires updating the worker trust file and the phone
configuration through that channel. Give the phone only the download key.

The build watcher forwards the existing checked release before examining the
source checkout, then forwards each successful new build. Remote failures have
durable independent backoff in `worker/build-state.json`; a restart or dirty
editable checkout does not require rebuilding the signed release. A matching
remote receipt is reused after a lost response. Existing configurations without
`remote_publication` keep their local-only behavior. Bootstrap or manually retry
the already checked local release without a build using:

```sh
python3 backend/dev_ota.py publish --config /private/path/config.json
```

## Publication API

Publication is disabled unless the server configuration explicitly provides
`publisher_token_file`, `certificate_sha256` and `build_tools` (the Kubernetes
entry point provisions these from the Secret). Send raw APK bytes to
`POST /updates/publish` with the publisher bearer key, one canonical
`Content-Length` bounded by 1 GiB, `X-OTA-Version-Code` containing a positive
Android versionCode, and `X-OTA-SHA256` containing its lowercase 64-character
SHA-256. Transfer encoding, duplicate headers and ambiguous metadata are
rejected before writing. Only one upload can validate at a time. Streaming uses
bounded buffers, a 30-second socket timeout and a 15-minute upload deadline.

After checking the request hash, the service invokes the same `stage()` path as
local publication: `apksigner verify`, expected signing certificate, `zipalign`,
`aapt` package/version inspection, and a verified private snapshot. Downgrades
and different bytes at an existing versionCode fail. Exact retries are
idempotent. Failed or interrupted uploads leave the previous release untouched.
Success atomically promotes the manifest and returns the matching receipt;
previous immutable downloads remain available. Requests, bearer keys, APK bytes
and tool diagnostic output are not logged.

Run focused synthetic publication tests locally:

```sh
python3 -m unittest discover -s backend -p 'test_dev_ota*.py' -q
python3 -m unittest discover -s backend -p 'test_release.py' -q
```
