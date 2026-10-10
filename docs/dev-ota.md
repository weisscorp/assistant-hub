# Development OTA service

`backend/dev_ota.py` adds an opt-in development channel alongside Hub. It serves
only authenticated HTTPS `GET /updates/manifest` and `GET /updates/<artifact>.apk`.
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
