"""Exercise real UTF-8 / terminal key bytes, rather than only Pilot events."""
import json
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='requires POSIX PTY')
ROOT = Path(__file__).resolve().parents[1]


def test_real_terminal_history_ghost_and_candidate_confirmation(tmp_path):
    import fcntl
    import pty
    import termios
    import tty
    master, slave = pty.openpty()
    tty.setraw(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 26, 100, 0, 0))
    state_file = tmp_path / 'state.json'
    env = dict(os.environ, TERM='xterm-256color', COLORTERM='truecolor',
               PYTHONPATH=os.pathsep.join((str(ROOT/'src'), str(ROOT/'packages/policy-sdk/src'))))
    env.pop('NO_COLOR', None)
    process = subprocess.Popen([sys.executable, str(ROOT/'tests/fixtures/completion_terminal.py'), str(state_file)],
                               stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
    os.close(slave)
    output = bytearray()
    def state():
        return json.loads(state_file.read_text()) if state_file.exists() else {}
    def wait(predicate, allow_exit=False):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if select.select([master], [], [], .03)[0]:
                try: output.extend(os.read(master, 65536))
                except OSError: pass
            if predicate(): return
            if not allow_exit:
                assert process.poll() is None, bytes(output[-3000:]).decode(errors='replace')
        raise AssertionError(f'terminal timeout: {state()}; tail={bytes(output[-1200:])!r}')
    def send(text):
        os.write(master, text.encode() if isinstance(text, str) else text)
    try:
        wait(lambda: state().get('width') == 100)
        history = 'policy example config set params.message 中文历史'
        send(history + '\r')
        wait(lambda: state().get('commands') == [history])
        prefix = 'policy example config set params.message 中'
        send(prefix)
        wait(lambda: state().get('ghost') == history)
        assert state()['value'] == prefix
        send(b'\x1b[C')
        wait(lambda: state().get('value') == history and not state().get('ghost'))
        assert state()['commands'] == [history]
        send(b'\x15')  # Ctrl+U clears actual input, not the history store.
        wait(lambda: state().get('value') == '')
        send('policy example config set params.mode ')
        wait(lambda: state().get('value') == 'policy example config set params.mode ')
        send(b'\t')
        wait(lambda: state().get('menu') and '普通' in state().get('items', []))
        assert state()['commands'] == [history]
        before = state()['selected']
        send(b'\x1b[C')
        wait(lambda: state().get('selected') != before)
        chosen = state()['items'][state()['selected']]
        send(' ')
        wait(lambda: not state().get('menu') and chosen in state().get('value', ''))
        assert state()['commands'] == [history]
        send('\r')
        wait(lambda: len(state().get('commands', [])) == 2)
        assert chosen in state()['commands'][-1]
        send('policy example config set params.mode ')
        wait(lambda: state().get('value') == 'policy example config set params.mode ')
        send(b'\t')
        wait(lambda: state().get('menu'))
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 42, 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        wait(lambda: state().get('width') == 42 and state()['panel_bottom'] <= state()['input_y'])
        assert state()['rows'] <= 6
        send(b'\x1b')
        wait(lambda: not state().get('menu'))
        send(b'\x15')
        wait(lambda: state().get('value') == '')
        send('policy li')
        wait(lambda: state().get('value') == 'policy li')
        send(b'\t')
        wait(lambda: state().get('menu') and state().get('items') == ['list'])
        send('\r')
        wait(lambda: not state().get('menu') and state()['value'].startswith('policy list'))
        assert len(state()['commands']) == 2
        send('\r')
        wait(lambda: len(state().get('commands', [])) == 3)
        send(b'\x11')
        wait(lambda: process.poll() is not None, allow_exit=True)
        assert process.returncode == 0
        assert b'\x1b[' in output and '中文历史'.encode() in output
        assert (tmp_path/'history.svg').exists() and (tmp_path/'menu.svg').exists()
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(3)
            except subprocess.TimeoutExpired: process.kill(); process.wait(3)
        os.close(master)
