"""Shared Hub memory and capabilities MCP with an explicit topic binding."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid
from agent_hub import uid, encoded
from hub_runtime import TOOLS
from hub_providers import request_json


def create_server(url, token_file, context, client, task_id=None):
    from mcp.server import Server
    from mcp.types import Tool, ToolAnnotations, CallToolResult, TextContent
    uid(context)
    if task_id is not None: uid(task_id)
    server = Server('assistant-hub-memory', version='0.2.0')
    definitions = [t['function'] for t in TOOLS if t['function']['name'] not in {'agent_task','context_switch'}]

    @server.list_tools()
    async def list_tools():
        nonlocal definitions
        if task_id:
            token=Path(token_file).read_text().strip()
            catalog=await asyncio.to_thread(request_json,url.rstrip('/')+'/api/tools?task='+task_id+'&context='+context,
                headers={'Authorization':'Bearer '+token})
            definitions=[t['function'] for t in catalog if t['function']['name'] not in {'agent_task','context_switch'}]
        return [Tool(name=t['name'],description=t['description'],inputSchema=t['parameters'],
            annotations=ToolAnnotations(readOnlyHint=t['name'] in {'memory_read','contexts_list','task_status','agents_list','capabilities_list','capability_result'},
                                        destructiveHint=t['name'] == 'capability_call' or t['name'].startswith('node_'),openWorldHint=False)) for t in definitions]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            if task_id and name.startswith('node_'):
                await list_tools()
            if name not in {t['name'] for t in definitions}: raise ValueError('Unknown tool')
            token = Path(token_file).read_text().strip()
            value = await asyncio.to_thread(request_json, url.rstrip('/')+'/api/tool',
                {'name':name, 'arguments':arguments, 'context_id':context, 'client':client,
                 'request_id':str(uuid.uuid4()), 'task_id':task_id},
                {'Authorization':'Bearer '+token})
            return CallToolResult(isError=isinstance(value,dict) and 'error' in value,
                                 content=[TextContent(type='text',text=encoded(value))])
        except (ValueError, OSError, KeyError):
            return CallToolResult(isError=True,content=[TextContent(type='text',text='Hub недоступен, тема не существует или аргументы инструмента неверны.')])
    return server


async def serve(args):
    from mcp.server.stdio import stdio_server
    server = create_server(args.url,args.token_file,args.context,args.client,args.task)
    async with stdio_server() as (read,write): await server.run(read,write,server.create_initialization_options())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True);parser.add_argument('--token-file',required=True)
    parser.add_argument('--context',required=True);parser.add_argument('--client',required=True)
    parser.add_argument('--task',help='Owned parent task for source-language knowledge indexing')
    args=parser.parse_args()
    try: asyncio.run(serve(args))
    except Exception as error:
        print('Hub MCP failed: '+type(error).__name__,file=sys.stderr);return 1
    return 0


if __name__ == '__main__': sys.exit(main())
