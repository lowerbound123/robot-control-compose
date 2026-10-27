"""Contract tests: real geometry, callback and HTTP adapters; no paid requests."""
import copy
import json
from pathlib import Path
import sys

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packages/llm-direction-policy/src'))
from rc_llm_direction.actions import Decision, make_chunk, parse_decision
from rc_llm_direction.configuration import DEFAULT_PARAMS, validate_config
from rc_llm_direction.runtime import DirectionPolicy


def manifest(**params):
    return {'execution': {'mode': 'sync', 'call_every_actions': 1, 'control_mode': 'epos'},
            'hot_reloadable': [], 'params': {**copy.deepcopy(DEFAULT_PARAMS), 'task': 'put the block in the tray', **params}}


def baseline():
    return np.array([.2, -.3, .4, .4, -.2, .7, -1., -.2, .3, .5, -.1, .4, -.3, -2.])


@pytest.mark.parametrize('motion,axis,sign', [
    ('forward', 0, 1), ('backward', 0, -1), ('left', 1, 1), ('right', 1, -1),
    ('up', 2, 1), ('down', 2, -1), ('twist-clockwise', 3, 1),
    ('twist-counterclockwise', 3, -1), ('tilt-down-toward-table', 4, 1),
    ('tilt-up-away-from-table', 4, -1), ('turn-left-across-table', 5, 1),
    ('turn-right-across-table', 5, -1),
])
def test_directions_agree_with_mcp_at_rotated_pose(motion, axis, sign):
    from robot_control.direct import DirectMotion
    from robot_control.direct_commands import parse_commands
    start = baseline()
    params = manifest()['params']
    result = make_chunk(start, Decision(motion, 'noop', False), params)
    direct = DirectMotion({}, 30, None, None, None)
    _, expected, _ = direct._resolve(parse_commands('left:' + motion)[0], {'eef': start, 'qpos': start})
    np.testing.assert_allclose(result[-1, :7], expected['left']['values'], atol=1e-7)
    np.testing.assert_array_equal(result[:, 7:], np.tile(start[7:].astype(np.float32), (30, 1)))
    if axis < 3:
        assert result[-1, axis] == pytest.approx(start[axis] + sign * .05)
        assert result[0, axis] == pytest.approx(start[axis] + sign * .05 / 30)
    else:
        relative = Rotation.from_euler('xyz', start[3:6]).inv() * Rotation.from_euler('xyz', result[0, 3:6])
        expected_delta = np.zeros(3)
        expected_delta[axis - 3] = sign * .1 / 30
        np.testing.assert_allclose(relative.as_rotvec(), expected_delta, atol=1e-7)
    np.testing.assert_array_equal(start, baseline())


def test_parallel_grippers_and_noop_use_baseline():
    start = baseline()
    params = manifest(chunk_size=4)['params']
    chunk = make_chunk(start, Decision('open', 'close', False), params)
    np.testing.assert_allclose(chunk[:, 6], [-1.6, -2.2, -2.8, -3.4])
    np.testing.assert_allclose(chunk[:, 13], [-1.5, -1., -.5, 0.])
    np.testing.assert_array_equal(chunk[:, :6], np.tile(start[:6].astype(np.float32), (4, 1)))
    held = make_chunk(start, Decision('noop', 'noop', False), params)
    np.testing.assert_array_equal(held, np.tile(start.astype(np.float32), (4, 1)))


@pytest.mark.parametrize('raw', [
    {'left': 'home', 'right': 'noop', 'done': False},
    {'left': 'up', 'right': 'noop', 'done': 'false'},
    {'left': 'up', 'right': 'noop', 'done': 0},
    {'left': 'up', 'done': False},
    {'left': 'up', 'right': 'noop', 'done': False, 'distance': 5},
])
def test_decision_rejects_extra_fields_and_non_categories(raw):
    with pytest.raises(ValueError):
        parse_decision(raw)


@pytest.mark.parametrize('patch', [
    {'task': ''}, {'request_when_remaining': -1}, {'request_when_remaining': 30},
    {'chunk_size': True}, {'translation_m': float('nan')}, {'rotation_rad': 0},
    {'jpeg_quality': 101}, {'image_max_side': 0}, {'unknown': 1},
])
def test_configuration_fails_before_opening_client(patch):
    with pytest.raises(ValueError):
        validate_config(manifest(**patch))


@pytest.mark.parametrize('name', ['PYTHONHOME', 'PYTHONPATH', 'CONDA_PREFIX', 'ROS_DOMAIN_ID', 'LD_PRELOAD'])
def test_credential_name_cannot_override_interpreter_environment(name):
    config = manifest()
    config['params']['provider']['api_key_env'] = name
    with pytest.raises(ValueError, match='api_key'):
        validate_config(config)


class Runtime:
    def __init__(self):
        self.pauses = []
    def pause(self, reason):
        self.pauses.append(reason)


class Provider:
    """Only the external model is replaced in these callback tests."""
    def __init__(self, response):
        self.response, self.calls, self.closed = response, [], False
    def decide(self, task, prompt, images):
        self.calls.append((task, prompt, images))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response
    def close(self):
        self.closed = True


def images():
    return {name: np.full((8, 12, 3), value, np.uint8)
            for name, value in zip(('head', 'left_wrist', 'right_wrist'), (25, 75, 125))}


def test_threshold_preserves_prefix_and_uses_tail_for_new_targets():
    provider = Provider(Decision('up', 'close', False))
    instance = DirectionPolicy(manifest(request_when_remaining=2, chunk_size=4), provider=provider)
    prefix = np.tile(baseline(), (3, 1)).astype(np.float32)
    runtime = Runtime()
    passed = instance.policy({'eef': [0] * 14}, prefix, {}, runtime)
    np.testing.assert_array_equal(passed, prefix)
    assert provider.calls == []
    prefix = prefix[:2]
    prefix[-1, 2] = .9
    result = instance.policy({'eef': [0] * 14}, prefix, images(), runtime)
    np.testing.assert_array_equal(result[:2], prefix)
    assert result.shape == (6, 14)
    assert result[-1, 2] == pytest.approx(.95)
    assert result[-1, 13] == 0
    assert len(provider.calls) == 1
    assert runtime.pauses == []


def test_empty_uses_feedback_and_aba_calls_model_once():
    provider = Provider(Decision('forward', 'noop', False))
    instance = DirectionPolicy(manifest(), provider=provider)
    runtime = Runtime()
    chunk = instance.policy({'eef': baseline()}, [], images(), runtime)
    assert chunk[-1, 0] == pytest.approx(.25)
    # Native-chain A -> pass-through B -> A, same instance and snapshot.
    output = instance.policy({'eef': baseline()}, chunk, images(), runtime)
    np.testing.assert_array_equal(output, chunk)
    assert len(provider.calls) == 1
    instance.reset_context()
    instance.close()
    assert provider.closed


def test_completion_requests_pause_and_returns_valid_placeholder():
    provider = Provider(Decision('noop', 'noop', True))
    instance = DirectionPolicy(manifest(), provider=provider)
    runtime = Runtime()
    result = instance.policy({'eef': baseline()}, [], images(), runtime)
    assert len(runtime.pauses) == 1
    assert result.shape == (1, 14) and np.isfinite(result).all()


def test_incomplete_camera_input_pauses_without_sending_request():
    provider = Provider(Decision('up', 'noop', False))
    instance = DirectionPolicy(manifest(), provider=provider)
    runtime = Runtime()
    result = instance.policy({'eef': baseline()}, [], {'head': images()['head']}, runtime)
    assert runtime.pauses and not provider.calls
    assert result.shape == (1, 14)
