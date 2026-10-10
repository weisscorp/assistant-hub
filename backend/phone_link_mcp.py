"""Device-scoped stdio MCP tools for an existing Hermes installation.

No listening port, credentials, model provider or modification of upstream Hermes.
Run with the official MCP SDK from requirements-mcp.txt. Mount the bridge's private
HomeLink state outside the editable checkout and explicitly select a device UUID.
"""
import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys

from home_link import HomeLinkStore, PhoneLinkTools, PHONE_LINK_SCHEMAS, identifier


class DeviceBinding:
    """Owner-selected binding; never infer a device from the knowledge database."""
    def __init__(self, device=None, device_file=None):
        if (device is None) == (device_file is None):
            raise ValueError('Select a device or a private binding file')
        self.device = identifier(device) if device is not None else None
        self.device_file = Path(device_file) if device_file is not None else None

    def resolve(self):
        if self.device is not None:
            return self.device
        # Reload at every call: owner pairing/revocation does not require a restart
        # and cannot leave a stale bound device cached in the running MCP process.
        with self.device_file.open('rb') as stream:
            data = stream.read(101)
        if len(data) > 100:
            raise ValueError('Invalid binding file')
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != {'device'}:
            raise ValueError('Invalid binding file')
        return identifier(value['device'])


def create_server(store, device=None, device_file=None):
    from mcp.server import Server
    from mcp.types import Tool, ToolAnnotations, CallToolResult, TextContent
    binding = DeviceBinding(device, device_file)
    server = Server('assistant-phone-link', version='1.0.0')

    @server.list_tools()
    async def list_tools():
        return [Tool(name=s['name'], description=s['description'], inputSchema=s['parameters'],
                     annotations=ToolAnnotations(readOnlyHint=s['name'] == 'phone_knowledge',
                                                 destructiveHint=False, openWorldHint=False,
                                                 idempotentHint=s['name'] == 'phone_knowledge'))
                for s in PHONE_LINK_SCHEMAS]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            tools = PhoneLinkTools(store, binding.resolve())
            return await asyncio.to_thread(tools.call, name, arguments)
        except (ValueError, KeyError, OSError):
            # Do not expose private URLs, arguments, paths or database errors in logs/errors.
            return CallToolResult(isError=True, content=[TextContent(type='text',
                text='Phone is not paired, knowledge or notification is unavailable for the owner-selected device, or arguments are invalid.')])

    return server


async def serve(store, device=None, device_file=None):
    from mcp.server.stdio import stdio_server
    server = create_server(store, device, device_file)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--device', help='Explicit synced phone UUID; never selected by the model')
    selection.add_argument('--device-file', type=Path, help='Private JSON {"device": UUID}, selected by the owner; missing file means unpaired')
    args = parser.parse_args()
    try:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        asyncio.run(serve(HomeLinkStore(args.state), args.device, args.device_file))
        return 0
    except (ValueError, OSError, ImportError):
        print('Phone-link MCP could not start. Check private state, device UUID and requirements-mcp.txt.', file=sys.stderr)
        return 1


if __name__ == '__main__': sys.exit(main())
