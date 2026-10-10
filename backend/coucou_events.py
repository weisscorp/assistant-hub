"""Opt-in, display-only adapter for Coucou's third-party agent event socket.

This is a local desktop component, not a LAN listener or a command executor.
It never reads approval decisions, installs hooks, retries or launches Coucou.
The shared coordinator's event source is not connected yet.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
import uuid


MAX_LINE_BYTES = 32768
EVENTS = {
    'session.started': 'SessionStart',
    'user.message': 'UserPromptSubmit',
    'tool.started': 'PreToolUse',
    'tool.succeeded': 'PostToolUse',
    'tool.failed': 'PostToolUseFailure',
    'question': 'Notification',
    'assistant.message': 'Stop',
    'task.failed': 'StopFailure',
    'task.cancelled': 'Interrupt',
    'session.ended': 'SessionEnd',
}
RESERVED_AGENTS = {'claude', 'claude-desktop', 'codex', 'cursor', 'hermes', 'copilot', 'muse'}


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('invalid UUID')
    return value


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError('invalid text')
    return value


def agent_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9-]{1,24}', value) or value in RESERVED_AGENTS:
        raise ValueError('invalid or reserved agent name')
    return value


def payload(event, agent='assistant'):
    """Project a closed assistant event schema onto upstream hook fields."""
    agent_name(agent)
    required = {'version', 'deviceId', 'conversationId', 'kind'}
    if not isinstance(event, dict) or not required <= set(event) <= required | {'text', 'tool'}:
        raise ValueError('invalid event fields')
    if type(event['version']) is not int or event['version'] != 1:
        raise ValueError('invalid version')
    device = _uuid(event['deviceId'])
    conversation = _uuid(event['conversationId'])
    kind = event['kind']
    if not isinstance(kind, str) or kind not in EVENTS:
        raise ValueError('invalid event kind')
    text = _text(event.get('text', ''), 4000)
    tool = _text(event.get('tool', ''), 80)
    if kind.startswith('tool.'):
        if not tool.strip() or tool == 'AskUserQuestion':
            raise ValueError('invalid display tool')
    elif 'tool' in event:
        raise ValueError('unexpected tool')
    if kind in {'user.message', 'assistant.message', 'question'} and not text.strip():
        raise ValueError('missing message')
    result = {
        'hook_event_name': EVENTS[kind],
        # Different devices must not resolve each other's session events.
        'session_id': 'assistant:' + device + ':' + conversation,
        'coucou_agent': agent,
    }
    if kind == 'user.message':
        result['prompt'] = text
    elif kind == 'assistant.message':
        result['last_assistant_message'] = text
    elif kind.startswith('tool.'):
        result['tool_name'] = tool
        # Generic display field; do not manufacture Bash/Edit inputs or diffs.
        result['tool_input'] = {'query': text} if text else {}
    elif kind == 'question':
        # Upstream generic Notification recognises a question by its final '?'.
        result['message'] = text.rstrip() if text.rstrip().endswith('?') else text.rstrip() + '?'
    elif text:
        result['message'] = text
    return result


def wire_event(event, agent='assistant'):
    data = (json.dumps(payload(event, agent), ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    if len(data) > MAX_LINE_BYTES:
        raise ValueError('oversized event')
    return data


def write_event(path, data, timeout=0.25):
    """Best-effort single write. True means written, not rendered or acknowledged."""
    path = Path(path)
    if not path.is_absolute() or not math.isfinite(timeout) or not 0 < timeout <= 5:
        raise ValueError('invalid local socket configuration')
    if not isinstance(data, bytes) or not data.endswith(b'\n') or len(data) > MAX_LINE_BYTES:
        raise ValueError('invalid wire event')
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(os.fspath(path))
            client.sendall(data)
        return True
    except OSError:
        # A partial write is uncertain. Retrying could duplicate a sound/state.
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='print hook JSON without connecting')
    mode.add_argument('--socket', type=Path, help='explicit existing local Coucou Unix socket')
    parser.add_argument('--agent', default='assistant', help='separate pill name; use a different name per device')
    parser.add_argument('--timeout', type=float, default=0.25)
    args = parser.parse_args(argv)
    try:
        agent_name(args.agent)
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 5 or args.socket is not None and not args.socket.is_absolute():
            raise ValueError('invalid configuration')
    except ValueError as error:
        parser.error(str(error))
    failed = False
    line_number = 0
    while True:
        raw = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        line_number += 1
        try:
            if len(raw) > MAX_LINE_BYTES:
                raise ValueError('oversized input')
            data = wire_event(json.loads(raw.decode('utf-8')), args.agent)
        except (ValueError, TypeError, UnicodeError):
            print(json.dumps({'line': line_number, 'status': 'invalid_event'}), file=sys.stderr)
            return 2
        if args.dry_run:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        else:
            written = write_event(args.socket, data, args.timeout)
            failed |= not written
            # Never log the prompt, tool arguments or response here.
            print(json.dumps({'line': line_number, 'status': 'written' if written else 'unavailable'}), file=sys.stderr)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
