# Private state and security boundary

This is a self-hosted single-owner development service, not a hosted multi-user product.
It defaults to 127.0.0.1; an owner can explicitly bind a literal private/VPN
interface (see [transport](docs/capabilities.md)). It validates Host/Origin.
Browser tickets are short-lived and single-use; full API access requires the
owner's generated bearer token or a named client key. Named keys store only
digests and can be revoked locally, including their browser sessions/tickets.
Owner keys have full access. Webhook keys only publish incidents; MCP keys and
dot OAuth grants only access `/mcp` and its bounded tools for live requests.
There are no separate tenants or per-node grants.
Coucou has a separate limited chat capability URL accepted only on direct
loopback. None of these credentials are public configuration. Source publication
does not open a network listener.

Internet access requires an HTTPS reverse proxy with an explicit public origin
and trusted proxy IP configuration. Public cookies are Secure/HttpOnly/SameSite,
origin-bound, and cookie POSTs require a matching Origin. Authentication failures
are rate-limited with bounded in-memory state. See [deployment](docs/internet-access.md)
for key management, HAProxy headers and transport boundaries. This layer is API
authentication, not a replacement for proxy connection/rate limits or isolation
of trusted agent code.

The optional [dot bridge](docs/dot-integration.md) adds an explicit OAuth consent
page, exact ChatGPT redirect allowlisting, PKCE, resource-bound tokens and grant
revocation. Its public metadata/registration routes expose no Hub knowledge.
The private tunnel relay reads a dedicated MCP key from disk; tunnel access must
remain within the owner's intended OpenAI workspace/account. Event filters route
requests and do not provide a separate user's data isolation.

Callback secrets live in the private database. Delivery checks resolved public
addresses, pins the validated destination for TLS, signs exact payload bytes and
does not follow redirects. Revocation/cancellation stops new tools/deliveries,
but does not undo a device action already accepted by an executor.

Use HTTP only inside a trusted encrypted VPN; a LAN IP alone provides no transport
encryption. Node workers hold a full owner-access key and are trusted owner code;
per-node grants are not yet supported. They execute only their installed catalog,
not arbitrary model-supplied shell commands. Recovery marks dispatched actions
with a lost result as unknown and never automatically repeats them.

Keep the runtime directory outside a checkout, owned by you and mode 0700.
Config/token files use 0600. SQLite, logs, native agent profiles and phone snapshots
contain personal information. Application-level database encryption is not provided;
use appropriate disk/account protection. Avoid adding live captures or memory to
issues, screenshots, example files, pull requests or training datasets.

Cloud providers receive the bounded text/history/selected facts necessary for a
request. Configure them intentionally. Tool outputs and imported documents are
untrusted content; they must not grant permission to execute commands or contact
people. Local agent adapters constrain their toolsets, but broader custom command
providers run with the privileges of the owning account.

For a simple consistent backup, stop Hub, then copy the entire private runtime
state (including linked phone data) to another private/encrypted location. Native
provider profiles may need their own supported backup procedure. Do not copy a
live WAL database file alone. Test restores in a separate private directory and
on alternate ports, never over an active owner's state.

Before publishing, inspect the exact staged file list. The ignore rules are a
second defense, not a guarantee: exported chats or credentials can have any name.
Repository tests use synthetic fixtures and must not touch physical phones.
Report a vulnerability through the repository's private security reporting if
enabled; never put credentials or personal data into a public issue.
