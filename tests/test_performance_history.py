from types import SimpleNamespace

import numpy as np

from rc_policy_sdk.performance import Trace
from test_qwen35_history_gripper_config import entry
from test_qwen35_history_runtime import Backend, config, frames


def test_cached_history_reports_lock_history_and_cache_without_model_call(entry):
    module, manifest, _ = entry
    backend = Backend()
    context = module.HistoryPolicy(config(50), backend=backend, policy_config=manifest)
    trace = Trace.start('run:cached:29')
    runtime = SimpleNamespace(consumed_actions=0, control_epoch=1, config_version=1, performance=trace)
    old = np.ones((29,14), np.float32)
    try:
        result = context.step({'qpos':[0.]*14}, old, frames(), runtime=runtime)
        np.testing.assert_array_equal(result, old)
        assert not backend.calls
        points = {p['stage']:p for p in trace.data['points']}
        assert {'history.lock_acquired','history.update','history.snapshot','history.cache'} <= points.keys()
        assert points['history.cache']['remaining_actions'] == 29
        assert 'history.model' not in points
        assert points['history.lock_acquired']['wait_ns'] >= 0
    finally:
        context.close()
