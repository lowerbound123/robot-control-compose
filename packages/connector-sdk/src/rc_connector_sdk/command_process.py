"""Run an opt-in connector's command socket in a fresh interpreter."""
import fcntl
import importlib.util
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

from .server import ConnectorServer
from .wire import receive_message, send_message


def load_source(path):
    source = Path(path).resolve()
    sys.path.insert(0, str(source.parent))
    spec = importlib.util.spec_from_file_location('connector_source', source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FeedbackWatermarks:
    """One feedback writer; receipts sample both sequences and their expiry together."""
    record = struct.Struct('=QQd')

    def __init__(self, fd):
        self.fd = fd

    def update(self, sequences, valid_until):
        data = self.record.pack(sequences['left'], sequences['right'], valid_until)
        # POSIX record locks are per-process even for an inherited descriptor.
        fcntl.lockf(self.fd, fcntl.LOCK_EX)
        try:
            if os.pwrite(self.fd, data, 0) != len(data):
                raise OSError('incomplete feedback watermark write')
        finally:
            fcntl.lockf(self.fd, fcntl.LOCK_UN)

    def read(self):
        fcntl.lockf(self.fd, fcntl.LOCK_SH)
        try:
            left, right, valid_until = self.record.unpack(os.pread(self.fd, self.record.size, 0))
            return {'left': left, 'right': right} if time.monotonic() <= valid_until else {}
        finally:
            fcntl.lockf(self.fd, fcntl.LOCK_UN)


class CommandProcess:
    def __init__(self, source, config, watermarks, startup_timeout):
        self.source, self.config = str(source), config
        self.watermarks, self.startup_timeout = watermarks, startup_timeout
        self.ready = threading.Event()
        self.control = self.process = None

    def start(self, listener, stop):
        self.startup_deadline = time.monotonic() + self.startup_timeout
        self.control, child = socket.socketpair()
        with child:
            fds = (listener.fileno(), child.fileno(), self.watermarks.fd)
            self.process = subprocess.Popen(
                [sys.executable, '-m', 'rc_connector_sdk.command_process', self.source, *map(str, fds)],
                pass_fds=fds)

        def monitor():
            try:
                if receive_message(self.control) == {'ready': True}:
                    self.ready.set()
                else:
                    stop.set()
            except (EOFError, OSError, ValueError):
                stop.set()
            finally:
                self.process.wait()
                stop.set()
        self.control.settimeout(self.startup_timeout)
        threading.Thread(target=monitor, name='connector-command-monitor', daemon=True).start()
        send_message(self.control, self.config)

    def wait_ready(self, stop):
        send_message(self.control, {'activate': True})
        while not self.ready.is_set():
            if stop.wait(.01):
                raise RuntimeError('connector command process stopped during startup')
            if time.monotonic() >= self.startup_deadline:
                raise TimeoutError('connector command process startup timed out')
        if stop.is_set():
            raise RuntimeError('connector stopped during startup')

    def close(self, deadline):
        try:
            if self.process is None: return True
            try:
                send_message(self.control, {'deadline': deadline})
                return self.process.wait(timeout=max(0., deadline - time.monotonic())) == 0
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                # The monitor reaps the child. A forced exit never counts as clean.
                self.process.poll()
                return False
        finally:
            if self.control is not None: self.control.close()


def main():
    source, listener_fd, control_fd, feedback_fd = sys.argv[1:]
    with socket.socket(fileno=int(listener_fd)) as listener, socket.socket(fileno=int(control_fd)) as control:
        # The inherited listening socket is nonblocking at the OS level.
        listener.settimeout(.1)
        config = receive_message(control)
        watermarks = FeedbackWatermarks(int(feedback_fd))
        driver = load_source(source).create_split_connector(
            config, role='command', watermarks=watermarks)
        server = ConnectorServer(driver, '')
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: server.stop.set())
        return server.serve_commands(listener, control, watermarks.read)


if __name__ == '__main__':
    raise SystemExit(main())
