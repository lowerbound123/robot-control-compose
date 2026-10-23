import io
import queue
import signal
import threading

import pytest

from robot_control import cli


class FakeController:
    instances = []

    def __init__(self, config_path):
        self.config_path = config_path
        self.events = queue.Queue()
        self.done = threading.Event()
        self.exit_code = 0
        self.commands = []
        self.starts = 0
        self.instances.append(self)

    def start(self):
        self.starts += 1
        self.events.put({"time": "now", "level": "INFO", "message": "ready"})

    def submit(self, line):
        self.commands.append(line)
        if line in ("exit", "quit"):
            self.events.put({"level": "INFO", "message": "cleaned up"})
            self.done.set()


def test_headless_preserves_json_and_eof_runs_cleanup(monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_controller", FakeController)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO('set policy.params.label "hello world"\nstatus\n'))
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    assert cli.main(["--config", "custom.json", "--headless"]) == 0
    controller = FakeController.instances[-1]
    assert controller.config_path == "custom.json"
    assert controller.starts == 1
    assert controller.commands == ['set policy.params.label "hello world"', "status", "exit"]
    assert "ready" in capsys.readouterr().out
    assert all(signal.getsignal(sig) == old for sig, old in previous.items())


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signals_request_same_controller_exit(monkeypatch, signum):
    class SignalController(FakeController):
        def start(self):
            super().start()
            signal.getsignal(signum)(signum, None)

    monkeypatch.setattr(cli, "make_controller", SignalController)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    assert cli.main(["--headless"]) == 0
    assert FakeController.instances[-1].commands == ["exit"]


def test_tui_exception_still_waits_for_cleanup(monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_controller", FakeController)

    def fail(controller, *, router=None):
        raise RuntimeError("terminal unavailable")

    monkeypatch.setattr(cli, "run_tui", fail)
    assert cli.main([]) == 1
    controller = FakeController.instances[-1]
    assert controller.commands == ["exit"]
    assert controller.done.is_set()
    assert "terminal unavailable" in capsys.readouterr().err


def test_controller_failure_exit_code_is_returned(monkeypatch):
    class FailedController(FakeController):
        def start(self):
            super().start()
            self.exit_code = 4
            self.done.set()

    monkeypatch.setattr(cli, "make_controller", FailedController)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    assert cli.main(["--headless"]) == 4


def test_headless_does_not_wait_for_blocked_stdin_after_shutdown():
    entered = threading.Event()
    unblock = threading.Event()

    class BlockingInput:
        def __iter__(self):
            entered.set()
            unblock.wait()
            return iter(())

    controller = FakeController("rc.json")
    controller.start()

    def finish():
        assert entered.wait(1)
        controller.events.put({"level": "INFO", "message": "shutdown complete"})
        controller.done.set()

    closer = threading.Thread(target=finish)
    closer.start()
    stdout = io.StringIO()
    try:
        cli.run_headless(controller, stdin=BlockingInput(), stdout=stdout)
        assert "shutdown complete" in stdout.getvalue()
    finally:
        unblock.set()
        closer.join(1)


def test_log_batches_yield_and_keep_all_final_events():
    controller = FakeController("rc.json")
    for count in range(600):
        controller.events.put({"level": "INFO", "message": f"line {count}"})
    stdout = io.StringIO()
    assert cli.drain_events(controller, stdout)
    assert len(stdout.getvalue().splitlines()) == 256
    controller.done.set()
    cli.run_headless(controller, stdin=io.StringIO(""), stdout=stdout)
    assert len(stdout.getvalue().splitlines()) == 600
