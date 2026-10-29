"""Exercise terminal modes and mouse bytes, rather than injecting Pilot events."""
from config_helpers import read_config
import base64
import json
import os
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires a POSIX PTY")
ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = '{"state":"paused","runtime":{"phase":"INITIALIZING","target":"qwen35_single"}}'


@pytest.mark.parametrize("reattach", ["resize", "command"])
@pytest.mark.parametrize("pixels", [False, True], ids=["cells", "pixels"])
def test_late_terminal_can_expand_and_copy_json_after_missing_startup_modes(tmp_path, reattach, pixels):
    import fcntl
    import pty
    import termios
    import tty

    state_file = tmp_path / "state.json"
    master, slave = pty.openpty()
    tty.setraw(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", NO_COLOR="1",
               PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT / "packages/policy-sdk/src"))))
    process = subprocess.Popen([sys.executable, str(ROOT / "tests/fixtures/tui_terminal.py"), str(state_file)],
                               stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    os.close(slave)
    output = bytearray()

    def receive():
        if select.select([master], [], [], .03)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                pass

    def observation():
        return read_config(state_file) if state_file.exists() else {}

    def wait_for(predicate, *, allow_exit=False):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            receive()
            if predicate():
                return
            if not allow_exit:
                assert process.poll() is None, output.decode(errors="replace")[-4000:]
        raise AssertionError(f"terminal did not reach expected state: {observation()}; output={bytes(output)[-1500:]!r}")

    def mouse_enabled():
        # A newly attached terminal only sees bytes emitted since attachment.
        modes = set()
        for numbers, action in re.findall(rb"\x1b\[\?([0-9;]+)([hl])", output):
            for number in numbers.split(b";"):
                if action == b"h":
                    modes.add(int(number))
                else:
                    modes.discard(int(number))
        return bool(modes & {1006, 1016}) and bool(modes & {1000, 1002, 1003})

    def pixel_encoding():
        # Ghostty uses the last enabled encoding: 1006h switches back to cells
        # even after 1016h. Do not model these as independent mode flags.
        formats = re.findall(rb"\x1b\[\?(1006|1016)h", output)
        return bool(formats and formats[-1] == b"1016")

    def click(x, y):
        assert mouse_enabled(), "the attached terminal was never told to send mouse input"
        if pixel_encoding():
            x, y = x * 10 + 5, y * 20 + 10  # Center of a 10x20 terminal cell.
        os.write(master, f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m".encode())

    try:
        wait_for(lambda: observation().get("ready") and mouse_enabled())
        if pixels:
            # Negotiate the real driver's in-band size / pixel mouse support.
            os.write(master, b"\x1b[?2048;1$y\x1b[48;24;80;480;800t")
            wait_for(lambda: b"\x1b[?1016h" in output)
        # Drain startup output as Docker does while the application is detached.
        drain_until = time.monotonic() + .3
        while time.monotonic() < drain_until:
            receive()
        output.clear()
        if reattach == "resize":
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
            if pixels:
                os.write(master, b"\x1b[48;32;100;640;1000t")
            else:
                os.kill(process.pid, signal.SIGWINCH)
        else:
            # Reattaching at the same size need not generate SIGWINCH. The
            # operator requests status, without restarting the running RC.
            os.write(master, b"status\r")
        wait_for(mouse_enabled)
        if reattach == "resize":
            wait_for(lambda: observation().get("width", 0) > 80)
        state = observation()
        click(state["x"], state["y"])
        wait_for(lambda: observation().get("expanded"))
        state = observation()
        click(state["copy_x"], state["y"])
        encoded = base64.b64encode(PAYLOAD.encode())
        wait_for(lambda: b"\x1b]52;c;" + encoded + b"\a" in output)
        assert observation()["expanded"]
        wait_for(lambda: observation().get("clipboard") == PAYLOAD)
        os.write(master, b"\x11")  # Ctrl+Q still follows the app's exit path.
        wait_for(lambda: process.poll() is not None, allow_exit=True)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        os.close(master)
