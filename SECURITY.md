# Private state and security boundary

This is a local single-owner development service, not a hosted multi-user product.
It defaults to 127.0.0.1; an owner can explicitly bind a literal private/VPN
interface (see [transport](docs/capabilities.md)). It validates Host/Origin.
Browser tickets are short-lived
and single-use; full API access requires the owner's generated bearer token.
Coucou has a separate limited chat capability URL. None of these values are
public configuration. Source publication does not open a network listener.

Use HTTP only inside a trusted encrypted VPN; a LAN IP alone provides no transport
encryption. Node workers hold the owner's full token and are trusted owner code;
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
