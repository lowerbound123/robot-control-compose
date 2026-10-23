import subprocess
import sys


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
