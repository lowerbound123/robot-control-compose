import pytest

from robot_control.policy_monitors import PolicyMonitors


class Worker:
    supports_monitor = True
    config_version = 1
    monitor_started_at = None
    def __init__(self, clock):
        self.clock = clock
        self.requests = []
        self.response = None
    def submit_monitor(self, epoch):
        if self.monitor_started_at is not None: return False
        self.requests.append(epoch)
        self.monitor_started_at = self.clock()
        return True
    def poll_monitor(self):
        response, self.response = self.response, None
        return response
    def finish(self, values=None, error=None):
        self.response = dict(type="monitor_error" if error else "monitor_result",
                             monitor_epoch=self.requests[-1], config_version=self.config_version,
                             result=values or {}, error=error)
        self.monitor_started_at = None


def setup():
    clock = [0.]
    worker = Worker(lambda: clock[0])
    state = [{"A":worker}, False]
    hub = PolicyMonitors(lambda: tuple(state), clock=lambda: clock[0], threaded=False)
    return hub, worker, state, clock


def test_only_enabled_at_10hz_and_snapshots_replace_keys():
    hub, worker, state, clock = setup()
    hub.tick()
    assert worker.requests == []
    assert hub.set_enabled("A", None)
    for i in range(10):
        clock[0] = i / 10
        hub.tick()
        worker.finish({"count":i})
    assert len(worker.requests) == 10
    clock[0] = .99
    hub.tick()
    assert hub.snapshot()["A"]["values"] == {"count":9}
    clock[0] = 1.01
    hub.tick()
    worker.finish({"new":"text"})
    hub.tick()
    assert hub.snapshot()["A"]["values"] == {"new":"text"}
    assert not hub.set_enabled("A", None)
    clock[0] = 2
    hub.tick()
    assert hub.snapshot() == {}


def test_timeout_preserves_last_value_and_does_not_repeat_call():
    hub, worker, state, clock = setup()
    hub.set_enabled("A", True)
    hub.tick()
    worker.finish({"ok":1})
    clock[0] = .1
    hub.tick()
    clock[0] = 2
    hub.tick()
    assert len(worker.requests) == 2
    assert hub.snapshot()["A"]["status"] == "timeout"
    assert hub.snapshot()["A"]["values"] == {"ok":1}
    worker.finish({"ok":2})
    hub.tick()
    assert hub.snapshot()["A"]["values"] == {"ok":2}


def test_off_on_lifecycle_and_reload_drop_late_values():
    hub, worker, state, clock = setup()
    hub.set_enabled("A", True)
    hub.tick()
    hub.set_enabled("A", False)
    hub.set_enabled("A", True)
    hub.tick()
    assert len(worker.requests) == 1
    worker.finish({"stale":1})
    clock[0] = .1
    hub.tick()
    assert hub.snapshot()["A"]["values"] is None
    hub.invalidate()
    state[1] = True
    worker.finish({"before_reset":1})
    hub.tick()
    assert hub.snapshot()["A"]["values"] is None
    state[1] = False
    replacement = Worker(lambda: clock[0])
    state[0] = {"A":replacement}
    hub.tick()
    assert replacement.requests
    assert hub.snapshot()["A"]["values"] is None
    state[0] = {}
    hub.tick()
    assert hub.snapshot() == {}


def test_unsupported_and_unknown_and_failure_only_in_panel():
    hub, worker, state, clock = setup()
    with pytest.raises(ValueError, match="未加载"):
        hub.set_enabled("missing", True)
    worker.supports_monitor = False
    with pytest.raises(ValueError, match="monitor"):
        hub.set_enabled("A", True)
    worker.supports_monitor = True
    hub.set_enabled("A", True)
    hub.tick()
    worker.finish(error="bad value")
    hub.tick()
    assert hub.snapshot()["A"]["error"] == "bad value"


@pytest.mark.parametrize('desired', [None, True, False])
def test_repeated_source_monitor_requires_an_explicit_instance(desired):
    from pathlib import Path
    first, second = Worker(lambda: 0), Worker(lambda: 0)
    first.policy_dir = second.policy_dir = Path('/policies/a')
    hub = PolicyMonitors(lambda: ({'a-0': first, 'a-1': second}, False), threaded=False)
    with pytest.raises(ValueError, match='a-0.*a-1'):
        hub.set_enabled('a', desired)
    assert hub.snapshot() == {}
    assert hub.set_enabled('a-0', True)
    assert list(hub.snapshot()) == ['a-0']
