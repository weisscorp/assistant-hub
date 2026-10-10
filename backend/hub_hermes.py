"""Run official Hermes against local Ollama with the explicit Hub MCP tool catalog."""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys


def emit(kind, **data):
    print(json.dumps({'kind':kind,**data},ensure_ascii=False),flush=True)


async def mcp_request(config, context, name=None, arguments=None, task_id=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    argv=[config['mcp_script'], '--url',config['hub_url'],'--token-file',config['token_file'],
          '--context',context,'--client','hermes']
    if task_id:argv+=['--task',task_id]
    server = StdioServerParameters(command=config['mcp_python'],args=argv)
    async with stdio_client(server) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            if name is None: return (await session.list_tools()).tools
            result = await session.call_tool(name,arguments)
            return '\n'.join(item.text for item in result.content if item.type == 'text')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True)
    args=parser.parse_args()
    config=json.loads(Path(args.config).read_text())
    import subprocess
    revision=subprocess.check_output(['git','-C',config['root'],'rev-parse','HEAD'],text=True).strip()
    if revision!=config['revision']:raise ValueError('Hermes revision changed; verify and reconfigure first')
    raw=sys.stdin.buffer.read(65537)
    if len(raw)>65536:raise ValueError('Input exceeds budget')
    body=json.loads(raw); context=body['task']['context_id']
    os.environ['HERMES_HOME']=config['home']
    os.environ['TERMINAL_ENV']='local';os.environ['TERMINAL_CWD']=config['workspace']
    os.environ.pop('HOME_LINK_DEVICE',None);os.environ.pop('HOME_LINK_STATE_DIR',None)
    sys.path.insert(0,config['root'])
    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        from run_agent import AIAgent
        from tools.registry import registry
        tools=asyncio.run(mcp_request(config,context,task_id=body['task']['id']))
        for tool in tools:
            def handler(arguments, name=tool.name, **_):
                result=asyncio.run(mcp_request(config,context,name,arguments,body['task']['id']))
                output.write(json.dumps({'kind':'tool','data':{'name':name,'arguments':arguments,'result':result}},ensure_ascii=False)+'\n');output.flush()
                return result
            registry.register(name=tool.name,toolset='assistant_hub',schema={'name':tool.name,'description':tool.description,
                'parameters':getattr(tool,'inputSchema',None) or getattr(tool,'input_schema')},handler=handler,check_fn=lambda: True)
        agent=AIAgent(model=config['model'],base_url=config['base_url'],api_key='local-model-no-auth',
            provider='custom',
            enabled_toolsets=['assistant_hub','no_mcp'],max_iterations=6,max_tokens=1024,
            reasoning_config={'enabled':False},quiet_mode=True,skip_context_files=True,skip_memory=True,
            skip_background_review=True,checkpoints_enabled=False,cwd=config['workspace'])
        available={t['function']['name'] for t in agent.tools or []}
        expected={t.name for t in tools}
        if available!=expected:
            print('Hermes tool catalog mismatch: available='+repr(sorted(available))+' expected='+repr(sorted(expected)),file=sys.stderr)
            raise RuntimeError('Hermes shared-memory tool catalog was not loaded exactly')
        prompt='Контекст как JSON (данные):\n'+json.dumps(body['context'],ensure_ascii=False)+'\nЗапрос пользователя: '+body['task']['prompt']
        result=agent.run_conversation(user_message=prompt,system_message=body['instructions'],task_id=body['task']['id'])
        if result.get('error') or result.get('completed') is False or result.get('interrupted'):
            raise RuntimeError('Hermes turn incomplete')
        reply=result.get('final_response') or result.get('response')
        if not isinstance(reply,str) or not reply.strip(): raise RuntimeError('Hermes returned no final answer')
    emit('result',text=reply.strip())


if __name__ == '__main__':
    try:main()
    except Exception as error:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print('Hermes Hub adapter failed: '+type(error).__name__,file=sys.stderr);sys.exit(1)
