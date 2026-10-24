"""AO verification: fixtures by default; opt in to loopback tests and a live GET."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from ao_overview import AOOverview


def verify(network=False, full_suite=False):
    command = [sys.executable, '-m', 'unittest']
    if full_suite:
        command += ['discover', '-s', 'backend', '-p', 'test_*.py', '-q']
    else:
        command += ['backend.test_ao_overview', 'backend.test_mini_tasks', 'backend.test_ao_verification']
        if network:
            command += ['backend.test_hub_hermes_api']
        command += ['-q']
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(ROOT / 'backend') + os.pathsep + environment.get('PYTHONPATH', '')
    tests = subprocess.run(command, cwd=ROOT, env=environment).returncode
    summary = {'tests_exit_code': tests, 'loopback_tests': 'run' if network else 'not_run',
               'live_ao': {'result': 'not_run'}}
    if network:
        # One on-demand session-list GET, even if synthetic tests failed.
        # Never print identities, prompts, updates, URLs or raw errors.
        try:
            snapshot = AOOverview().snapshot()
            summary['live_ao'] = {
                'result': snapshot['source_status'],
                'checked_at': snapshot['checked_at'],
                'last_success_at': snapshot['last_success_at'],
                'session_count': len(snapshot['tasks']),
            }
            if 'error' in snapshot:
                summary['live_ao']['error'] = snapshot['error']
        except ValueError:
            summary['live_ao'] = {'result': 'invalid_configuration'}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if tests == 0 and (not network or summary['live_ao']['result'] == 'available') else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network', action='store_true',
                        help='Run synthetic loopback HTTP tests and one live read-only AO list GET')
    parser.add_argument('--full-suite', action='store_true',
                        help='Run the full backend suite instead of focused tests (requires --network)')
    args = parser.parse_args()
    if args.full_suite and not args.network:
        parser.error('--full-suite requires --network because existing tests bind loopback sockets')
    return verify(args.network, args.full_suite)


if __name__ == '__main__':
    raise SystemExit(main())
