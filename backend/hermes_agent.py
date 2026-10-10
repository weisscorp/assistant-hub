"""One queued request -> a pinned Hermes library invocation. No shell interpolation.

Run with the interpreter prepared by Hermes PM. This adapter is not a sandbox:
the service account/container must enforce workspace and credential isolation.
"""
import argparse
import json
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
from urllib.parse import urlsplit
import uuid

HERMES_REVISION = 'bb2362878b33e14e8b7d24d621510e087269fdce'
class AgentIncomplete(ValueError): pass
INSTRUCTIONS = '''Ты домашний Hermes голосового помощника. Отвечай по-русски,
кратко, чтобы ответ можно было произнести в гарнитуре. Для изменения приложения
работай в указанной рабочей папке, прочитай её инструкции и проверь изменения.
Не утверждай, что обновление выпущено или установлено, без проверенного результата
доверенного сервиса выпуска. Не меняй ключи подписи, конфигурацию сервера и этот
адаптер. Если задача не завершена, явно скажи, что осталось сделать.'''


def verify_revision(root, revision):
    root = root.resolve(strict=True)
    if not root.is_dir(): raise ValueError('invalid Hermes checkout')
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('expected a pinned Hermes commit')
    actual = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], timeout=10, text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=normal'], timeout=10, text=True)
    if actual != revision or dirty:
        raise ValueError('Hermes checkout differs from the configured revision')
    return root


def prepare(root, revision, workspace, home, base_url, model):
    root = verify_revision(root, revision)
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir() or not model.strip():
        raise ValueError('invalid agent configuration')
    endpoint = urlsplit(base_url)
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or not endpoint.hostname:
        raise ValueError('invalid model endpoint')
    if endpoint.scheme != 'https' and not (endpoint.scheme == 'http' and endpoint.hostname in ('localhost', '127.0.0.1', '::1')):
        raise ValueError('use HTTPS for a remote model endpoint')
    home = home.absolute()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if home.is_symlink() or not home.is_dir():
        raise ValueError('invalid private Hermes home')
    info = home.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Hermes home must be owned by the service account with mode 0700')
    home = home.resolve()
    if home == workspace or workspace in home.parents or home == root or root in home.parents:
        raise ValueError('keep agent state outside the editable checkout')
    os.environ['HERMES_HOME'] = str(home)
    os.environ['TERMINAL_ENV'] = 'local'
    os.environ['TERMINAL_CWD'] = str(workspace)
    sys.path.insert(0, str(root))
    return workspace


@contextmanager
def diagnostics_to_stderr():
    """Also redirect native/child stdout; only the final reply goes to queue stdout."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def converse(factory, workspace, text, model, base_url, api_key, iterations, seconds, task_id):
    toolsets = ['terminal', 'file']
    if os.environ.get('HOME_LINK_STATE_DIR'):
        from home_link import HomeLinkStore, register_hermes_tools
        store = HomeLinkStore(os.environ['HOME_LINK_STATE_DIR'])
        devices = store.devices()
        device = os.environ.get('HOME_LINK_DEVICE') or (devices[0]['device'] if len(devices) == 1 else None)
        if device:
            register_hermes_tools(store, device)
            toolsets.append('phone_link')
    agent = factory(model=model, provider='custom', api_mode='chat_completions',
                    base_url=base_url, api_key=api_key, cwd=str(workspace),
                    enabled_toolsets=toolsets, quiet_mode=True,
                    save_trajectories=False, skip_background_review=True, skip_memory=True,
                    load_soul_identity=False, max_iterations=iterations, run_budget_seconds=seconds,
                    ephemeral_system_prompt=INSTRUCTIONS)
    try:
        result = agent.run_conversation(user_message=text, task_id=task_id)
        if not isinstance(result, dict) or result.get('completed') is not True or any(
                result.get(name) for name in ('failed', 'partial', 'interrupted', 'error')):
            flags = {name: bool(result.get(name)) for name in ('completed', 'failed', 'partial', 'interrupted', 'error')} if isinstance(result, dict) else {}
            raise AgentIncomplete(f'Hermes did not complete the request: {flags}')
        reply = result.get('final_response')
        if not isinstance(reply, str) or not reply.strip():
            raise AgentIncomplete('Hermes returned no final reply')
        return reply.strip()[:1500]
    finally:
        agent.close()


def voice_turn(factory, workspace, body, model, base_url, api_key):
    from conversation import validate, response, INSTRUCTIONS as VOICE_INSTRUCTIONS
    validate(body)
    toolsets = ['web']
    if body.get('phoneDevice') and os.environ.get('HOME_LINK_STATE_DIR'):
        from home_link import HomeLinkStore, register_hermes_tools
        register_hermes_tools(HomeLinkStore(os.environ['HOME_LINK_STATE_DIR']), body['phoneDevice'])
        toolsets.extend(['phone_link', 'vision'])
    agent = factory(model=model, provider='custom', api_mode='chat_completions',
                    base_url=base_url, api_key=api_key, cwd=str(workspace),
                    enabled_toolsets=toolsets, quiet_mode=True, save_trajectories=False,
                    skip_background_review=True, skip_memory=True, load_soul_identity=False,
                    max_iterations=4, run_budget_seconds=28, ephemeral_system_prompt=VOICE_INSTRUCTIONS)
    try:
        result = agent.run_conversation(user_message=json.dumps(body, ensure_ascii=False), task_id=body['id'])
        if not isinstance(result, dict) or result.get('completed') is not True or any(result.get(key) for key in ('failed', 'partial', 'interrupted', 'error')):
            raise AgentIncomplete('Hermes did not complete the voice turn')
        value = json.loads(result['final_response'])
        response(value, body)
        return json.dumps(value, ensure_ascii=False)
    finally:
        agent.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-root', type=Path, required=True)
    parser.add_argument('--hermes-revision', default=HERMES_REVISION)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--max-iterations', type=int, default=40)
    parser.add_argument('--seconds', type=int, default=1500)
    parser.add_argument('--conversation', action='store_true')
    args = parser.parse_args()
    try:
        # Bridge still caps user speech at 2000 characters; internal repair prompts
        # also include bounded build diagnostics and the original request.
        text = sys.stdin.buffer.read(65537 if args.conversation else 32001).decode('utf-8')
        if not text.strip() or len(text.encode()) > (65536 if args.conversation else 32000) or (not args.conversation and len(text) > 8000):
            raise ValueError('invalid request length')
        if not 1 <= args.max_iterations <= 100 or not 1 <= args.seconds <= 3600:
            raise ValueError('invalid agent budget')
        key = os.environ.get('HELMET_MODEL_API_KEY')
        if not key:
            raise ValueError('HELMET_MODEL_API_KEY is required')
        task_id = os.environ.get('HELMET_JOB_ID', str(uuid.uuid4()))
        if str(uuid.UUID(task_id)) != task_id:
            raise ValueError('invalid job identifier')
        with diagnostics_to_stderr():
            workspace = prepare(args.hermes_root, args.hermes_revision, args.workspace, args.home, args.base_url, args.model)
            from run_agent import AIAgent
            if args.conversation:
                reply = voice_turn(AIAgent, workspace, json.loads(text), args.model, args.base_url, key)
            else:
                # Serializes coding with the trusted build runner in this checkout.
                with (workspace / '.release-build.lock').open('a+b') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    reply = converse(AIAgent, workspace, text, args.model, args.base_url, key,
                                     args.max_iterations, args.seconds, task_id)
        print(reply)
    except Exception as error:
        # Provider exceptions may contain credentials or the entire prompt.
        print(str(error) if isinstance(error, AgentIncomplete) else
              f'Hermes request failed ({type(error).__name__}); inspect private server diagnostics.', file=sys.stderr)
        for frame in traceback.extract_tb(error.__traceback__)[-5:]:
            print(f'  {Path(frame.filename).name}:{frame.lineno} in {frame.name}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
