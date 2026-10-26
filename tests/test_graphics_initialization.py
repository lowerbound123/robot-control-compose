import subprocess
import sys

import pytest


def test_importing_tui_never_probes_terminal():
    # Library widget imports query terminal input; importing our UI must not.
    subprocess.run([sys.executable, "-c", """
import sys
import robot_control.tui
assert 'textual_image.widget' not in sys.modules
"""], check=True)


def test_headless_graphics_probe_reports_unavailable(monkeypatch):
    from robot_control import graphics
    monkeypatch.setattr(graphics.sys, "__stdout__", None)
    support = graphics.probe_graphics("sixel")
    assert support.protocol == "sixel" and support.error


@pytest.mark.skipif(sys.platform == "win32", reason="requires a POSIX PTY")
@pytest.mark.parametrize("protocol", ["kitty", "sixel"])
@pytest.mark.parametrize("winsize", [(0, 0, 0, 0), (0, 80, 800, 0), (24, 0, 0, 480)])
def test_uninitialized_pty_uses_default_cells_without_terminal_responses(protocol, winsize):
    import fcntl
    import os
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *winsize))
        # Fresh imports and a real PTY reproduce detached container startup.
        # Nothing reads the master or replies to graphics capability queries.
        result = subprocess.run([sys.executable, "-c", """
import sys
from robot_control.graphics import probe_graphics
from textual_image import _terminal

support = probe_graphics(sys.argv[1])
assert support.error is None, support
assert support.protocol == sys.argv[1]
assert support.cell_size == (10, 20), support
assert 'textual_image.widget' in sys.modules

def unexpected_probe():
    raise AssertionError('cell size must stay cached after Textual takes stdin')
_terminal.get_tiocgwinsz = unexpected_probe
assert _terminal.get_cell_size() == support.cell_size
""", protocol], stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                                text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    finally:
        os.close(slave)
        os.close(master)


@pytest.fixture
def attached_terminal(monkeypatch):
    from types import SimpleNamespace
    from textual_image import _terminal
    from textual_image.renderable import sixel, tgp

    # Preserve any cache created by other tests, including its absence.
    monkeypatch.setattr(_terminal.get_cell_size, "_result", None, raising=False)
    monkeypatch.delattr(_terminal.get_cell_size, "_result")
    monkeypatch.setattr(sys, "__stdout__", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(_terminal, "get_tiocgwinsz", lambda: (30, 100, 800, 480))
    monkeypatch.setattr(sixel, "query_terminal_support", lambda: True)
    monkeypatch.setattr(tgp, "query_terminal_support", lambda: True)
    return _terminal, {"kitty": tgp, "sixel": sixel}


@pytest.mark.parametrize("protocol", ["kitty", "sixel"])
def test_attached_terminal_preserves_measured_cells(protocol, attached_terminal):
    from robot_control.graphics import probe_graphics

    support = probe_graphics(protocol)
    assert support.error is None
    assert support.cell_size == (8, 16)


@pytest.mark.parametrize("protocol", ["kitty", "sixel"])
def test_attached_terminal_still_rejects_unsupported_protocol(protocol, attached_terminal, monkeypatch):
    from robot_control.graphics import probe_graphics

    _, backends = attached_terminal
    monkeypatch.setattr(backends[protocol], "query_terminal_support", lambda: False)
    support = probe_graphics(protocol)
    assert f"终端不支持配置的 {protocol} 图像协议" in support.error


@pytest.mark.parametrize("cached_size", [(0, 0), (0, 20), (10, 0)])
def test_invalid_cached_cells_are_replaced_before_widget_import(cached_size, attached_terminal, monkeypatch):
    from robot_control.graphics import probe_graphics

    terminal, _ = attached_terminal
    monkeypatch.setattr(terminal.get_cell_size, "_result", terminal.CellSize(*cached_size), raising=False)
    support = probe_graphics("kitty")
    assert support.error is None
    assert support.cell_size == (10, 20)
    assert terminal.get_cell_size() == (10, 20)


def test_protocol_division_error_is_not_treated_as_missing_terminal_size(attached_terminal, monkeypatch):
    from robot_control.graphics import probe_graphics

    _, backends = attached_terminal
    def broken_protocol():
        raise ZeroDivisionError("protocol failure")
    monkeypatch.setattr(backends["kitty"], "query_terminal_support", broken_protocol)
    support = probe_graphics("kitty")
    assert support.error == "相机协议初始化失败：protocol failure"
