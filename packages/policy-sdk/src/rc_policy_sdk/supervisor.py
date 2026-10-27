"""Linux subreaper that outlives user code and cleans escaped descendants."""

import argparse
import os
import signal
import subprocess
import sys
import time

from .lifecycle import become_subreaper, descendant_snapshot, process_birth, signal_processes


def _reap(worker, children):
    worker.poll()
    for pid in children:
        if pid == worker.pid:
            continue
        if process_birth(pid) != children[pid]:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            pass


def _cleanup(worker, known=None):
    records = dict(known or {})
    signaled = set()
    term_deadline = time.monotonic() + 3.5
    kill_deadline = term_deadline + 1.0
    while True:
        records.update(descendant_snapshot(os.getpid()))
        _reap(worker, records)
        records = {pid: birth for pid, birth in records.items() if process_birth(pid) == birth}
        if not records:
            # Subreaper adoption happens before waitpid reports worker exit.
            if worker.poll() is not None and not descendant_snapshot(os.getpid()):
                return True
        now = time.monotonic()
        killing = now >= term_deadline
        pending = {pid: birth for pid, birth in records.items() if killing or (pid, birth) not in signaled}
        signal_processes(pending, signal.SIGKILL if killing else signal.SIGTERM)
        signaled.update(pending.items())
        if now >= kill_deadline:
            _reap(worker, records)
            remaining = {pid for pid, birth in records.items() if process_birth(pid) == birth}
            if remaining:
                print(f"policy supervisor could not reap descendants: {sorted(remaining)}", file=sys.stderr, flush=True)
            return not remaining and worker.poll() is not None and not descendant_snapshot(os.getpid())
        time.sleep(.02)


def run(socket_path, policy_dir):
    # This process imports no user policy. It remains alive after os._exit in
    # the worker, so even an immediately detached child has a known parent.
    become_subreaper()
    shutdown_signal = 0

    def request_shutdown(signum, _frame):
        nonlocal shutdown_signal
        shutdown_signal = signum

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    if shutdown_signal:
        return 128 + shutdown_signal
    worker = subprocess.Popen([sys.executable, "-m", "rc_policy_sdk.worker", "--socket", socket_path, "--policy-dir", policy_dir])
    try:
        while worker.poll() is None and not shutdown_signal:
            time.sleep(.02)
        status = 128 + shutdown_signal if shutdown_signal else worker.returncode
    finally:
        cleaned = _cleanup(worker)
    if not cleaned:
        return 1
    return 128 - status if status < 0 else status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--policy-dir", required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.socket, args.policy_dir))


if __name__ == "__main__":
    main()
