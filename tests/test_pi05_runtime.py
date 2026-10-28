import importlib
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest

from pi05_helpers import checkpoint_fixture
from test_qwen35_runtime import Model, images, metadata

PACKAGE = Path(__file__).resolve().parents[1] / 'packages/pi05-policy/src'
if importlib.util.find_spec('rc_pi05_policy') is None:
    sys.path.insert(0, str(PACKAGE))


def implementation():
    assert importlib.util.find_spec('rc_pi05_policy.runtime') is not None, 'Pi05 runtime missing'
    return importlib.import_module('rc_pi05_policy.runtime').AsyncPi05Policy


def settings(tmp_path, size=50):
    from rc_pi05_policy.configuration import load_model_config
    path, *_ = checkpoint_fixture(tmp_path, horizon=size)
    return path, load_model_config(path, project_root=tmp_path)


def step(policy, chunk, count):
    return policy.step({'qpos': np.arange(14)}, chunk, images(), runtime=metadata(count))


def test_pi05_and_qwen_share_alignment_and_blending_but_not_gripper_offset(tmp_path):
    cls = implementation()
    from rc_qwen35_policy.runtime import AsyncQwenPolicy
    _, cfg = settings(tmp_path)
    pi_model, qw_model = Model(), Model()
    for model in (pi_model, qw_model): model.output[:, [6, 13]] = -1
    pi = cls(cfg, backend=pi_model, project_root=tmp_path)
    qw = AsyncQwenPolicy({'model': {'workflow': 'single'}, 'runtime': cfg['runtime'],
                          'execution': cfg['execution']}, backend=qw_model)
    try:
        pi_chunk, qw_chunk = step(pi, [], 0), step(qw, [], 0)
        for model in (pi_model, qw_model): model.output[:] = 10
        pi_chunk = step(pi, pi_chunk[21:], 21)
        qw_chunk = step(qw, qw_chunk[21:], 21)
        pi.pending.result(1)
        qw.pending.result(1)
        pi_chunk = step(pi, pi_chunk[7:], 28)
        qw_chunk = step(qw, qw_chunk[7:], 28)
        joints = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]
        np.testing.assert_array_equal(pi_chunk[:, joints], qw_chunk[:, joints])
        assert pi_chunk.shape == (43, 14) and pi.last_trim == qw.last_trim == 7
        np.testing.assert_equal(pi_chunk[:22, [6, 13]], -1)
        np.testing.assert_equal(pi_chunk[22:, [6, 13]], 10)
        np.testing.assert_allclose(qw_chunk[:, [6, 13]], pi_chunk[:, [6, 13]] + .15)
    finally:
        pi.close()
        qw.close()


def test_pi05_uses_checkpoint_execution_length_and_reset_without_reloading(tmp_path):
    cls = implementation()
    _, cfg = settings(tmp_path, size=10)
    model = Model()
    model.chunk_size = 10
    model.output = np.zeros((10, 14), np.float32)
    policy = cls(cfg, backend=model)
    try:
        assert step(policy, [], 0).shape == (10, 14)
        policy.reset()
        model.output[:] = 2
        np.testing.assert_equal(step(policy, [], 10), 2)
        assert [kind for kind, _ in model.events].count('load') == 1
    finally: policy.close()


def test_switch_model_only_validates_and_returns_a_cold_path_patch(tmp_path):
    cls = implementation()
    path, cfg = settings(tmp_path)
    model = Model()
    policy = cls(cfg, backend=model, project_root=tmp_path)
    try:
        assert policy.switch_model('model.json') == {'params.model_config_path': str(path)}
        assert [kind for kind, _ in model.events] == ['load', 'warmup']
        with pytest.raises(FileNotFoundError): policy.switch_model('absent.json')
    finally: policy.close()
