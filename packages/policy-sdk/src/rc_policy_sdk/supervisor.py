"""Linux subreaper that outlives user code and cleans escaped descendants."""

import os
import signal
import sys
import time

from .lifecycle import descendant_snapshot, process_birth, signal_processes


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
