"""Fixed-config launcher for Hermes' PM-managed dependency interpreter.
The input request is inherited stdin, never an argument or shell program.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from hermes_agent import HERMES_REVISION, prepare


def launch(config, conversation=False):
    path = config.resolve(strict=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
        raise ValueError('configuration must be private and owned by the service account')
    data = json.loads(path.read_text())
    root = Path(data['hermes_root'])
    revision = data.get('hermes_revision', HERMES_REVISION)
    home = Path(data['home'])
    workspace = prepare(root, revision, Path(data['workspace']), home, data['base_url'], data['model'])
    root = root.resolve()
    environment = dict(os.environ)
    environment['HERMES_RUNTIME_DIR'] = str(Path(data['runtime_dir']).resolve(strict=True))
    bootstrap = Path(data['bootstrap_python'])
    # Import PM only after verifying the fixed upstream checkout.
    composed = json.loads(subprocess.check_output([str(bootstrap), '-m', 'pm.environments', '--format', 'json'],
                         cwd=root, env=environment, text=True, timeout=30))
    environment.update(composed)
    python = subprocess.check_output([str(bootstrap), '-c',
             'from pathlib import Path; from pm.environments import selected_venv,venv_python; print(venv_python(selected_venv(Path.cwd())))'],
             cwd=root, env=environment, text=True, timeout=30).strip()
    # Do not resolve this symlink: Python uses the venv path to select its dependencies.
    if not Path(python).is_file(): raise ValueError('Hermes dependency interpreter is not installed')
    arguments = [python, str(Path(__file__).with_name('hermes_agent.py')),
                 '--hermes-root', str(root), '--hermes-revision', revision,
                 '--workspace', str(workspace), '--home', str(home.resolve()),
                 '--base-url', data['base_url'], '--model', data['model'],
                 '--max-iterations', str(data.get('max_iterations', 40)), '--seconds', str(data.get('seconds', 1500))]
    if conversation: arguments.append('--conversation')
    os.execve(python, arguments, environment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--conversation', action='store_true')
    args = parser.parse_args()
    try: launch(args.config, args.conversation)
    except Exception as error:
        print(f'Hermes launcher failed ({type(error).__name__}); check private configuration and PM installation.', file=sys.stderr)
        return 1


if __name__ == '__main__': sys.exit(main())
