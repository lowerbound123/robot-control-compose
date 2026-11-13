from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import pytest

from rc_policy_sdk.transitions import TrajectoryAddTransition
from test_qwen35_history_gripper_config import entry
from test_qwen35_history_runtime import Backend, config, frames


def runtime(count, epoch=1):
    return SimpleNamespace(consumed_actions=count, control_epoch=epoch, config_version=1)


@pytest.fixture
def context(entry):
    module, manifest, _ = entry
    models = []
    def create(pipelined=False):
        backend = Backend()
        backend.history_slot_count, backend.history_slot_stride = 4, 1
        policy = module.HistoryPolicy(config(45, pipelined), backend=backend, policy_config=manifest)
        models.append((policy, backend))
        return policy, backend
    yield create
    for policy, backend in models:
        backend.gate.set()
        policy.close()


@pytest.mark.parametrize('pipelined', [False, True])
def test_policy_returns_supplied_remainder_unchanged_until_its_threshold(context, pipelined):
    policy, backend = context(pipelined)
    old = np.arange(6 * 14, dtype=np.float32).reshape(6, 14)
    old.flags.writeable = False
    result = policy.step({'qpos': np.zeros(14)}, old, frames(), runtime=runtime(100))
    np.testing.assert_array_equal(result, old)
    assert not backend.calls
    assert not np.shares_memory(result, old)


@pytest.mark.parametrize('pipelined', [False, True])
def test_one_due_request_waits_for_inference_then_optionally_fuses(context, pipelined):
    policy, backend = context(pipelined)
    old = np.full((5, 14), -.5, dtype=np.float32)
    backend.output[:, :6] = 2.
    backend.gate.clear()
    state = {'qpos': np.zeros(14)}
    with ThreadPoolExecutor(1) as caller:
        future = caller.submit(policy.step, state, old, frames(), runtime=runtime(100))
        assert backend.started.wait(1)
        assert not future.done()
        # Independent observations must not trim the response's request-time origin.
        policy.observe(state, frames(11), runtime=runtime(102))
        policy.observe(state, frames(22), runtime=runtime(104))
        backend.gate.set()
        result = future.result(2)
    raw = TrajectoryAddTransition(5., .25).apply(old, backend.output) if pipelined else backend.output
    expected = policy._process(raw, threshold=policy.default_gripper_threshold)
    np.testing.assert_allclose(result, expected)
    assert len(backend.calls) == 1 and policy.pending is None


def test_history_observations_continue_and_request_snapshot_excludes_future_frames(context):
    policy, backend = context()
    state = {'qpos': np.zeros(14)}
    policy.observe(state, frames(0), runtime=runtime(100))
    for count in range(101, 105):
        policy.observe(state, frames(count-100), runtime=runtime(count))
    policy.step(state, [], frames(99), runtime=runtime(102))
    observed = backend.calls[-1][3]
    assert [item.index for item in observed] == [0, 1]
    assert [int(item.image.flat[0]) for item in observed] == [1, 2]
    assert policy.monitor()['history_tick'] == 4
