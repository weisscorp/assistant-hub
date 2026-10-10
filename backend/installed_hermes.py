"""Fixed-config adapter for a vendor-installed Hermes runtime without Git/PM.

Run with that installation's Python. Source fingerprint is pinned by the owner;
updating the installed image requires compatibility verification and a new pin.
Voice turns and queued coding jobs retain their existing separate contracts.
"""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import uuid
from urllib.parse import urlsplit

from hermes_agent import AgentIncomplete, diagnostics_to_stderr, voice_turn, converse
from phone_link_mcp import DeviceBinding


def source_fingerprint(root):
    root = Path(root).resolve(strict=True)
    entries = []
    for path in sorted(root.rglob('*.py')):
        relative = path.relative_to(root)
        if any(p in ('.venv', '.git', '__pycache__') for p in relative.parts):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('Invalid installed source tree')
        entries.append(str(relative) + ' ' + hashlib.sha256(path.read_bytes()).hexdigest())
    if not entries:
        raise ValueError('Installed Hermes source is missing')
    return len(entries), hashlib.sha256('\n'.join(entries).encode()).hexdigest()


def model_endpoint(value, allow_private_lan_http=False):
    if not isinstance(value, str) or type(allow_private_lan_http) is not bool:
        raise ValueError('Invalid model endpoint')
    url = urlsplit(value)
    if not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
        raise ValueError('Invalid model endpoint')
    _ = url.port  # Reject malformed ports before importing or calling the runtime.
    if url.scheme == 'https':
        return value.rstrip('/')
    if url.scheme == 'http':
        if url.hostname == 'localhost':
            return value.rstrip('/')
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            address = None
        if address and address.is_loopback:
            return value.rstrip('/')
        networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7')
        if allow_private_lan_http and address and any(address in ipaddress.ip_network(n) for n in networks):
            return value.rstrip('/')
    raise ValueError('Use HTTPS, loopback, or explicitly enabled private LAN IP')


def read_config(path):
    path = Path(path).resolve(strict=True)
    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    trusted_owner = info.st_uid == os.getuid() or (info.st_uid == 0 and info.st_gid == os.getgid())
    if not stat.S_ISREG(info.st_mode) or not trusted_owner or mode & 0o027 or info.st_size > 32768:
        raise ValueError('Configuration must be private and service-readable')
    data = json.loads(path.read_bytes())
    required = {'hermes_root', 'source_files', 'source_sha256', 'workspace', 'home', 'model', 'base_url'}
    optional = {'allow_private_lan_http', 'api_key_env', 'max_iterations', 'seconds', 'link_state', 'device_file'}
    if not isinstance(data, dict) or not required <= set(data) <= required | optional:
        raise ValueError('Invalid installed runtime configuration')
    if type(data['source_files']) is not int or data['source_files'] <= 0:
        raise ValueError('Invalid installed source pin')
    if not isinstance(data['source_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', data['source_sha256']):
        raise ValueError('Invalid installed source pin')
    for name in ('hermes_root', 'workspace', 'home', 'link_state', 'device_file'):
        if name in data and (not isinstance(data[name], str) or not Path(data[name]).is_absolute()):
            raise ValueError('Use fixed absolute paths')
    if ('link_state' in data) != ('device_file' in data):
        raise ValueError('Knowledge requires explicit state and owner binding')
    if not isinstance(data['model'], str) or not data['model'].strip() or len(data['model']) > 200:
        raise ValueError('Invalid model')
    data['base_url'] = model_endpoint(data['base_url'], data.get('allow_private_lan_http', False))
    for key, default, maximum in (('max_iterations', 40, 100), ('seconds', 1500, 3600)):
        value = data.get(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('Invalid agent budget')
    if 'api_key_env' in data and (not isinstance(data['api_key_env'], str) or not re.fullmatch('[A-Z][A-Z0-9_]{1,80}', data['api_key_env'])):
        raise ValueError('Invalid model credential configuration')
    return data


def check_local_model(data):
    """Fail before SDK retries when an explicitly local model has no listener.

    This only probes a TCP connection; no prompt, token or phone data is sent.
    A listener does not prove that a model is loaded or can answer a request.
    HTTPS providers retain the SDK's normal certificate validation and transport.
    """
    endpoint = urlsplit(model_endpoint(data['base_url'], data.get('allow_private_lan_http', False)))
    if endpoint.scheme != 'http':
        return
    try:
        with socket.create_connection((endpoint.hostname, endpoint.port or 80), timeout=2):
            pass
    except OSError:
        raise RuntimeError('Local model listener unavailable') from None


def prepare(data, body=None):
    root = Path(data['hermes_root']).resolve(strict=True)
    if source_fingerprint(root) != (data['source_files'], data['source_sha256']):
        raise ValueError('Installed Hermes changed; verify compatibility before repinning')
    workspace = Path(data['workspace']).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError('Workspace unavailable')
    home = Path(data['home'])
    if home.is_symlink():
        raise ValueError('Invalid private runtime home')
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = home.resolve(strict=True)
    if home.stat().st_uid != os.getuid() or stat.S_IMODE(home.stat().st_mode) != 0o700:
        raise ValueError('Runtime home must be private and owned by service user')
    if home == workspace or workspace in home.parents or home == root or root in home.parents:
        raise ValueError('Keep runtime home outside editable code')
    # Never inherit a default device or discover one by counting database rows.
    os.environ.pop('HOME_LINK_STATE_DIR', None)
    os.environ.pop('HOME_LINK_DEVICE', None)
    if 'device_file' in data:
        try:
            device = DeviceBinding(device_file=data['device_file']).resolve()
        except (FileNotFoundError, ValueError):
            device = None
        if body is not None and device and body.get('phoneDevice') not in (None, device):
            raise ValueError('Voice request belongs to a different phone')
        if device and (body is None or body.get('phoneDevice') == device):
            os.environ['HOME_LINK_STATE_DIR'] = data['link_state']
            os.environ['HOME_LINK_DEVICE'] = device
    os.environ['HERMES_HOME'] = str(home)
    os.environ['TERMINAL_ENV'] = 'local'
    os.environ['TERMINAL_CWD'] = str(workspace)
    sys.path.insert(0, str(root))
    return workspace


def run(factory, workspace, data, text, conversation=False):
    key_name = data.get('api_key_env', 'HELMET_MODEL_API_KEY')
    key = os.environ.get(key_name)
    if not key:
        # Local Ollama does not authenticate this compatibility value. Remote
        # providers require their explicitly configured environment credential.
        if urlsplit(data['base_url']).scheme == 'http':
            key = 'local-model-no-auth'
        else:
            raise ValueError('Model credential unavailable')
    if conversation:
        def voice_factory(**kwargs):
            kwargs.update(skip_context_files=True, max_tokens=1024,
                          reasoning_config={'enabled': False})
            # Only explicit phone handlers are permitted. Installed/default MCP
            # connectors must not silently expand a voice session's tool scope.
            kwargs['enabled_toolsets'] = [*kwargs['enabled_toolsets'], 'no_mcp']
            return factory(**kwargs)
        return voice_turn(voice_factory, workspace, json.loads(text), data['model'], data['base_url'], key)
    task = os.environ.get('HELMET_JOB_ID', str(uuid.uuid4()))
    if str(uuid.UUID(task)) != task:
        raise ValueError('Invalid job identifier')
    with (workspace / '.release-build.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return converse(factory, workspace, text, data['model'], data['base_url'], key,
                        data.get('max_iterations', 40), data.get('seconds', 1500), task)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--conversation', action='store_true')
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(65537 if args.conversation else 32001)
        text = raw.decode('utf-8')
        if not text.strip() or len(raw) > (65536 if args.conversation else 32000) or (not args.conversation and len(text) > 8000):
            raise ValueError('Invalid request length')
        body = None
        if args.conversation:
            from conversation import validate
            body = validate(json.loads(text))
        with diagnostics_to_stderr():
            data = read_config(args.config)
            check_local_model(data)
            workspace = prepare(data, body)
            from run_agent import AIAgent
            reply = run(AIAgent, workspace, data, text, args.conversation)
        print(reply)
        return 0
    except Exception as error:
        print('Installed Hermes request failed (' + type(error).__name__ + '); check private configuration and runtime compatibility.', file=sys.stderr)
        return 1


if __name__ == '__main__': sys.exit(main())
