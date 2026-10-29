"""POSIX descendant discovery and bounded cleanup for isolated workers."""

import ctypes
import errno
import os
from pathlib import Path
import platform
import signal
import sys
import time


class _BSDInfo(ctypes.Structure):
    # proc_bsdinfo from macOS sys/proc_info.h (PROC_PIDTBSDINFO flavor 3).
    _fields_ = [("ids", ctypes.c_uint32 * 12), ("comm", ctypes.c_char * 16),
                ("name", ctypes.c_char * 32), ("details", ctypes.c_uint32 * 6),
                ("start_seconds", ctypes.c_uint64), ("start_microseconds", ctypes.c_uint64)]


def process_birth(pid):
    """Stable birth marker, or None after exit; distinguishes recycled PIDs."""
    if sys.platform.startswith("linux"):
        try:
            data = Path(f"/proc/{pid}/stat").read_text()
            return int(data[data.rfind(")") + 2:].split()[19])
        except (FileNotFoundError, ProcessLookupError):
            return None
    if sys.platform == "darwin":
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
        libproc.proc_pidinfo.restype = ctypes.c_int
        info = _BSDInfo()
        ctypes.set_errno(0)
        size = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size == 0 and ctypes.get_errno() in (0, errno.ESRCH, errno.ENOENT):
            return None
        if size == 0 and ctypes.get_errno() == errno.EPERM:
            # macOS may refuse metadata for a just-exited sandbox child. An
            # ESRCH liveness probe confirms exit; live EPERM remains a failure.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return None
        if size != ctypes.sizeof(info):
            raise OSError(ctypes.get_errno() or errno.EINVAL, "cannot identify process birth")
        return (info.start_seconds, info.start_microseconds)
    raise OSError("process birth tracking requires Linux or macOS")


def become_subreaper():
    if sys.platform.startswith("linux"):
        # Adopt orphaned grandchildren so this worker can reap them before exit.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot enable child subreaper")


def descendant_snapshot(pid, root_birth=None):
    """Descendant PID -> birth, with Linux ancestry/birth from one stat read."""
    if sys.platform.startswith("linux"):
        processes = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                data = (entry / "stat").read_text()
                fields = data[data.rfind(")") + 2:].split()
                processes[int(entry.name)] = (int(fields[1]), int(fields[19]))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, ValueError, IndexError) as exc:
                raise RuntimeError("cannot enumerate process tree") from exc
        if root_birth is not None and (pid not in processes or processes[pid][1] != root_birth):
            return {}
        found, frontier = {}, {pid}
        while frontier:
            frontier = {child for child, (parent, _) in processes.items() if parent in frontier and child not in found}
            found.update({child: processes[child][1] for child in frontier})
        return found
    if root_birth is not None and process_birth(pid) != root_birth:
        return {}
    if sys.platform == "darwin":
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_listchildpids.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
        libproc.proc_listchildpids.restype = ctypes.c_int
        found, frontier = set(), {pid}
        while frontier:
            children = set()
            for parent in frontier:
                capacity = max(16, libproc.proc_listchildpids(parent, None, 0) + 16)
                buffer = (ctypes.c_int * capacity)()
                count = libproc.proc_listchildpids(parent, buffer, ctypes.sizeof(buffer))
                if count < 0:
                    raise OSError(ctypes.get_errno(), "cannot discover policy children")
                children.update(child for child in buffer[:count] if child > 0 and child not in found)
            found.update(children)
            frontier = children
        return {child: birth for child in found if (birth := process_birth(child)) is not None}
    raise OSError("descendant discovery requires Linux or macOS")


def _pidfd_syscall(number, *arguments):
    # Linux assigns these modern syscall numbers identically on ARM64/x86_64.
    # Some Conda CPython builds omit their Python wrappers even on new kernels.
    if not sys.platform.startswith("linux") or platform.machine().lower() not in {"aarch64", "arm64", "x86_64", "amd64"}:
        raise OSError(errno.ENOSYS, "native pidfd fallback requires Linux ARM64 or x86_64")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(number), *arguments)
    if result == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _open_pidfd(pid):
    builtin = getattr(os, "pidfd_open", None)
    if builtin is not None:
        return builtin(pid, 0)
    return _pidfd_syscall(434, ctypes.c_int(pid), ctypes.c_uint(0))


def _send_pidfd_signal(descriptor, signum):
    builtin = getattr(signal, "pidfd_send_signal", None)
    if builtin is not None:
        builtin(descriptor, signum, None, 0)
        return
    _pidfd_syscall(424, ctypes.c_int(descriptor), ctypes.c_int(signum), ctypes.c_void_p(None), ctypes.c_uint(0))


def signal_processes(records, signum):
    """Signal only the captured process; pidfd prevents Linux PID reuse races."""
    for pid, birth in records.items():
        if sys.platform.startswith("linux"):
            try:
                descriptor = _open_pidfd(pid)
            except ProcessLookupError:
                continue
            try:
                # Opening a pidfd and checking birth are deliberately in this
                # order: the signal targets the pinned process, never a later
                # process reusing the numeric PID after this verification.
                if process_birth(pid) == birth:
                    try:
                        _send_pidfd_signal(descriptor, signum)
                    except ProcessLookupError:
                        pass
            finally:
                os.close(descriptor)
            continue
        try:
            if process_birth(pid) == birth:
                os.kill(pid, signum)
        except ProcessLookupError:
            pass


def reap_children():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                return
        except ChildProcessError:
            return


def cleanup_worker_children():
    records = {}
    for signum in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + .75
        signaled = set()
        while True:
            reap_children()
            records.update(descendant_snapshot(os.getpid()))
            records = {pid: birth for pid, birth in records.items() if process_birth(pid) == birth}
            if not records:
                return
            pending = {pid: birth for pid, birth in records.items() if (pid, birth) not in signaled}
            signal_processes(pending, signum)
            signaled.update(pending.items())
            if time.monotonic() >= deadline:
                break
            time.sleep(.02)
    reap_children()
    if any(process_birth(pid) == birth for pid, birth in records.items()):
        raise RuntimeError("worker descendants remain after cleanup deadline")
