"""Real worker transport and lifecycle for composed policies; no robot motion."""

import json
from pathlib import Path
import sys

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool
from robot_control.config import PolicyRegistry
from host_helpers import LocalPolicyProcess as PolicyProcess
from test_process import wait_response


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["qpos", "epos", "mixed"])
def test_chain_hold_example_runs_hot_updates_and_resets_in_real_worker(tmp_path, mode):
    record = PolicyRegistry(ROOT / "tests/fixtures/policies").get("chain_hold")
    config = record.config
    config["execution"]["control_mode"] = mode
    config["params"].update(chunk_size=6, max_steps=3)
    worker = PolicyProcess(record.directory, config, python_command=[sys.executable],
                           init_timeout_s=3, inference_timeout_s=3, release_timeout_s=1)
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 3, 3), np.uint8), 1.0)
    lease = pool.acquire()
    try:
        worker.start()
        call = {"request_id": 1, "generation": 0, "config_version": 1,
                "state": {"qpos": [0.2] * 14, "eef": [0.4] * 14},
                "action_chunk": {"actions": [], "modes": []} if mode == "mixed" else [],
                "images": lease.descriptors}
        worker.submit(call)
        result = wait_response(worker)
        assert result["type"] == "result", result
        expected_modes = ["qpos", "epos", "qpos"] if mode == "mixed" else [mode] * 3
        assert result["modes"] == expected_modes
        expected = [[0.2 if item == "qpos" else 0.4] * 14 for item in expected_modes]
        np.testing.assert_allclose(result["actions"], expected)

        pid = worker.pid
        worker.update_config({"params.max_steps": 2}, 2)
        worker.reset_context()
        worker.submit({**call, "request_id": 2, "config_version": 2})
        result = wait_response(worker)
        assert result["type"] == "result" and len(result["actions"]) == 2, result
        assert worker.pid == pid
        with pytest.raises(RuntimeError, match="max_steps"):
            worker.update_config({"params.max_steps": 0}, 3)
        assert worker.config_version == 2
        assert worker.config["params"]["max_steps"] == 2
        # PolicyProcess closes on lifecycle errors for ordinary policies too.
        assert not worker.alive
    finally:
        unclean = worker.close()
        lease.release()
        pool.close()
    assert not unclean and not worker.alive


LIFECYCLE_POLICY = '''
import json
from rc_policy_sdk import PolicyChain

def record(config, event):
    with open(config['params']['events'], 'a') as stream:
        stream.write(json.dumps(event) + '\\n')

class Stage:
    def __init__(self, name):
        self.name = name
        self.config = None

    def initialize(self, config, *, runtime):
        self.config = config
        record(config, [self.name, 'initialize', runtime.config_version])
        if self.name == 'second' and config['params']['fail_init']:
            raise ValueError('load failed')
        return {'calls': 0, 'config': config}

    def policy(self, context, state, action_chunk, images, config, *, runtime):
        assert config is context['config']
        assert not images['head'].flags.writeable
        assert runtime.consumed_actions == 7
        context['calls'] += 1
        record(config, [self.name, 'policy', context['calls']])
        if self.name == 'first':
            return [[config['params']['value']] * 14]
        return [[value + context['calls'] for value in action_chunk[0]]]

    def reset_context(self, context):
        context['calls'] = 0
        record(self.config, [self.name, 'reset'])

    def release(self, context):
        record(self.config, [self.name, 'release', context is None])

chain = PolicyChain([Stage('first'), Stage('second')])
def initialize(config, *, runtime): return chain.initialize(config, runtime=runtime)
def policy(context, state, action_chunk, images, config, *, runtime):
    return chain.policy(context, state, action_chunk, images, config, runtime=runtime)
def reset_context(context): chain.reset_context(context)
def release(context): chain.release(context)
'''


@pytest.mark.parametrize("fail_init", [False, True])
def test_real_worker_chain_initialization_reset_and_cleanup(tmp_path, fail_init):
    directory = tmp_path / "policy"
    directory.mkdir()
    (directory / "main.py").write_text(LIFECYCLE_POLICY)
    events = tmp_path / "events.jsonl"
    config = {"policy_host": "test", "hot_reloadable": ["params.value"],
              "params": {"events": str(events), "fail_init": fail_init, "value": 2}}
    worker = PolicyProcess(directory, config, python_command=[sys.executable],
                           init_timeout_s=3, inference_timeout_s=3, release_timeout_s=1)
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 3, 3), np.uint8), 1.0)
    lease = pool.acquire()
    try:
        if fail_init:
            with pytest.raises(RuntimeError, match="stage 2.*load failed"):
                worker.start()
        else:
            worker.start()
            call = {"request_id": 1, "generation": 0, "config_version": 1,
                    "state": {}, "action_chunk": [], "images": lease.descriptors,
                    "runtime": {"consumed_actions": 7}}
            worker.submit(call)
            assert wait_response(worker)["actions"] == [[3.0] * 14]
            worker.update_config({"params.value": 5}, 2)
            worker.reset_context()
            worker.submit({**call, "request_id": 2, "config_version": 2})
            assert wait_response(worker)["actions"] == [[6.0] * 14]
    finally:
        worker.close()
        lease.release()
        pool.close()
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert rows[:2] == [["first", "initialize", 1], ["second", "initialize", 1]]
    assert [row for row in rows if row[1] == "release"] == [
        ["second", "release", fail_init], ["first", "release", False],
    ]
    if not fail_init:
        assert [row for row in rows if row[1] == "reset"] == [["first", "reset"], ["second", "reset"]]
    assert not worker.alive
