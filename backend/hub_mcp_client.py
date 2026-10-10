"""One-shot CLI access to the shared MCP tools, for native agent consoles."""
import argparse
import asyncio
import json
import sys
from pathlib import Path


async def run(args):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    argv=[str(Path(__file__).with_name('hub_mcp.py')),
        '--url',args.url,'--token-file',args.token_file,'--context',args.context,'--client',args.client]
    if args.task:argv+=['--task',args.task]
    params=StdioServerParameters(command=sys.executable,args=argv)
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as client:
            await client.initialize()
            reply=await client.call_tool(args.name,json.loads(args.arguments))
            print(json.dumps({'error':bool(reply.isError),'content':[c.text for c in reply.content if c.type=='text']},ensure_ascii=False))
            return 1 if reply.isError else 0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('url','token-file','context','client','name','arguments'):p.add_argument('--'+name,required=True)
    p.add_argument('--task')
    args=p.parse_args()
    return asyncio.run(run(args))


if __name__=='__main__':sys.exit(main())
