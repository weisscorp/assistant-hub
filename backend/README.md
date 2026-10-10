# Backend modules

The default entry point is `hub_server.py`; use `../tools/local_stack.py` to generate
private configuration and start the local stack. See the [project README](../README.md).

The core uses Python's standard library with SQLite FTS5. MCP wrappers additionally
need the pinned `requirements-mcp.txt` environment. Tests use temporary state and
synthetic providers. Earlier `bridge.py`, `installed_hermes.py`, job and release
modules are retained as optional building blocks and tested separately. They are
not launched by default. The opt-in [`dev_ota.py` service](../docs/dev-ota.md)
combines checked builds and release staging into a private development OTA channel.
