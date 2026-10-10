"""POSIX job supervisor: ends the entire agent process group on cancellation/parent death.
User request arrives through stdin; argv is administrator configuration, never request text.
"""
import argparse
import json
import os
import selectors
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lifeline-fd', type=int, required=True)
    parser.add_argument('--timeout', type=float, required=True)
    args = parser.parse_args()
    argv = json.loads(os.environ['HELMET_AGENT_ARGV'])
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item for item in argv):
        return 125
    stop = False

    def stopped(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    selector = selectors.DefaultSelector()
    selector.register(args.lifeline_fd, selectors.EVENT_READ)
    child = None
    result = 130
    try:
        if selector.select(0):  # Parent vanished before we could start the agent.
            return 130
        child = subprocess.Popen(argv, stdin=None, start_new_session=True, shell=False)
        deadline = time.monotonic() + args.timeout
        while True:
            code = child.poll()
            if code is not None:
                result = 0 if code == 0 else 1
                break
            if stop or selector.select(.05):
                result = 130
                break
            if time.monotonic() >= deadline:
                result = 124
                break
    finally:
        if child is not None:
            # Kill descendants even when the immediate agent has already exited.
            try:
                os.killpg(child.pid, signal.SIGTERM)
                time.sleep(.15)
                child.poll()  # Reap an exited leader before signalling remaining descendants.
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=3)
        selector.close()
        os.close(args.lifeline_fd)
    return result


if __name__ == '__main__':
    raise SystemExit(main())
