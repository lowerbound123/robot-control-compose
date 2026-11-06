"""Durable command history never performs filesystem work on the UI thread."""
import importlib.util
import json
import stat
import threading
from pathlib import Path

import pytest


def history_class():
    assert importlib.util.find_spec('robot_control.history'), 'persistent history is missing'
    from robot_control.history import CommandHistory
    return CommandHistory


def test_history_roundtrip_preserves_raw_text_deduplicates_and_isolates_configs(tmp_path):
    History = history_class()
    first = History(tmp_path / 'one.yaml', tmp_path / 'outputs')
    assert first.wait_idle(3)
    raw = '  global set note "quoted  中文"  '
    first.record(raw)
    first.record('status')
    first.record(raw)
    assert first.wait_idle(3)
    assert first.snapshot()[0] == ('status', raw)
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
    first.close()
    second = History(tmp_path / 'one.yaml', tmp_path / 'outputs')
    other = History(tmp_path / 'two.yaml', tmp_path / 'outputs')
    try:
        assert second.wait_idle(3) and other.wait_idle(3)
        assert second.snapshot()[0] == ('status', raw)
        assert other.snapshot()[0] == () and other.path != second.path
        for index in range(505):
            second.record(f'global set item {index}')
        assert second.wait_idle(3)
        lines, _ = second.snapshot()
        assert len(lines) == 500 and lines[0] == 'global set item 5' and lines[-1] == 'global set item 504'
        for invalid in ('not-a-command', 'global set api_key "never-save-this"'):
            with pytest.raises(ValueError):
                second.record(invalid)
        assert 'never-save-this' not in second.path.read_text()
    finally:
        second.close(); other.close()


def test_history_load_and_atomic_save_run_off_the_calling_thread(tmp_path, monkeypatch):
    History = history_class()
    owner = threading.get_ident()
    io_threads = []
    original_read = Path.read_text
    def read(path, *args, **kwargs):
        io_threads.append(threading.get_ident())
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    import robot_control.history as module
    original_replace = module.os.replace
    def replace(*args):
        io_threads.append(threading.get_ident())
        return original_replace(*args)
    monkeypatch.setattr(module.os, 'replace', replace)
    store = History(tmp_path / 'rc.yaml', tmp_path / 'outputs')
    try:
        store.record('status')
        assert store.wait_idle(3)
        assert io_threads and owner not in io_threads
    finally:
        store.close()


def test_bad_history_and_failed_save_are_advisory_and_preserve_previous_file(tmp_path, monkeypatch):
    History = history_class()
    store = History(tmp_path / 'rc.yaml', tmp_path / 'outputs')
    assert store.wait_idle(3)
    store.record('status'); assert store.wait_idle(3)
    original = store.path.read_bytes()
    import robot_control.history as module
    def denied(*args):
        raise PermissionError('do not leak filesystem details')
    monkeypatch.setattr(module.os, 'replace', denied)
    store.record('pause'); assert store.wait_idle(3)
    assert store.snapshot()[0] == ('status', 'pause')
    assert store.path.read_bytes() == original
    assert store.take_warning() and store.take_warning() is None
    store.close()
    store.path.write_text('{broken')
    broken = History(tmp_path / 'rc.yaml', tmp_path / 'outputs')
    try:
        assert broken.wait_idle(3)
        assert broken.snapshot()[0] == ()
        assert broken.take_warning()
        assert broken.path.read_text() == '{broken'
    finally:
        broken.close()


@pytest.mark.parametrize('stalled', [False, True])
def test_process_exit_flushes_last_submission_without_waiting_forever_for_filesystem(tmp_path, stalled):
    import os
    import subprocess
    import sys
    import time
    root = Path(__file__).resolve().parents[1]
    script = r'''
import asyncio, json, queue, sys, threading, time
from pathlib import Path
from robot_control.tui import RobotControlApp, CommandInput
from robot_control.history import CommandHistory
root, stalled = Path(sys.argv[1]), sys.argv[2] == 'True'
closing = threading.Event()
original_save = CommandHistory._save
def save(self, lines):
    assert closing.wait(3)
    if stalled: threading.Event().wait(20)
    else: time.sleep(.15)
    original_save(self, lines)
CommandHistory._save = save
class Controller:
    config_path, outputs = root / 'rc.yaml', root / 'outputs'
    events, done, exit_code = queue.Queue(), threading.Event(), 0
    def start(self): pass
    def submit(self, line): pass
    def status(self): return {'state':'paused'}
class App(RobotControlApp):
    async def on_unmount(self):
        closing.set()
        result = super().on_unmount()
        if result is not None: await result
async def main():
    app = App(Controller())
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.value = 'global set note "last submitted 中文"'
        await pilot.press('enter')
        app.controller.done.set()
        await pilot.pause(.08)
asyncio.run(main())
'''
    environment = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(root / 'src'), str(root / 'packages/policy-sdk/src')))}
    started = time.monotonic()
    child = subprocess.run([sys.executable, '-c', script, str(tmp_path), str(stalled)], env=environment,
                           text=True, capture_output=True, timeout=6)
    assert child.returncode == 0, child.stderr
    assert time.monotonic() - started < 4
    if not stalled:
        store = history_class()(tmp_path / 'rc.yaml', tmp_path / 'outputs')
        try:
            assert store.wait_idle(3)
            assert store.snapshot()[0] == ('global set note "last submitted 中文"',)
        finally:
            store.close()
