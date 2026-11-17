"""Dev preprocessing/coordinates with real tensors and only model forward replaced."""
import importlib
from types import SimpleNamespace

import numpy as np
import pytest

torch = pytest.importorskip('torch')
pytest.importorskip('transformers')

from pi05_helpers import write_json
from pi05_lift2_helpers import CAMERAS, checkpoint_fixture, local_tokenizer
from rc_pi05_lift2.configuration import load_model_config


class Predictor(torch.nn.Module):
    def __init__(self, config):
        super().__init__()
        self.batch = None
        self.output = torch.zeros(1, config.chunk_size, 32)

    def predict_action_chunk(self, batch):
        self.batch = batch
        return self.output.clone()

    def reset(self): pass


def frames():
    # Different colors and aspect ratios expose camera swaps, BGR, and padding.
    result = {}
    for i, (name, shape) in enumerate(zip(CAMERAS, [(2,4), (4,2), (3,3)])):
        frame = np.zeros((*shape, 3), np.uint8)
        frame[..., i] = 255
        frame.setflags(write=False)
        result[name] = frame
    return result


def prepared(tmp_path, monkeypatch, *, affine=False):
    path, raw, *_ = checkpoint_fixture(tmp_path, horizon=5, execute=3)
    if affine: raw['observations'].update(state_scale=[2.]*14, state_offset=[1.]*14)
    write_json(path, raw)
    local_tokenizer(tmp_path/'tokenizer')
    module = importlib.import_module('rc_pi05_lift2.backend')
    monkeypatch.setattr(module, 'load_local_policy', lambda checkpoint,cfg:Predictor(cfg))
    backend = module.Pi05Backend(load_model_config(path, project_root=tmp_path))
    backend.load()
    return backend


def test_rgb_resize_padding_masks_and_normalization_match_dev(tmp_path, monkeypatch):
    backend = prepared(tmp_path, monkeypatch)
    qpos = np.full(14, 3., np.float32)
    images = frames()
    try:
        output = backend.predict(qpos, images, 'pick_up\ncup')
        assert output.shape == (3,14)
        np.testing.assert_allclose(output[:, :6], 13.)
        np.testing.assert_allclose(output[:, 7:13], 13.)
        np.testing.assert_allclose(output[:, [6,13]], 10.)
        batch = backend.policy.batch
        torch.testing.assert_close(batch['observation.state'], torch.full((1,14),1/(1+0.5e-6)))
        assert batch['observation.language.tokens'].shape == (1,200)
        assert batch['observation.language.tokens'].dtype == torch.int64
        for i, target in enumerate(CAMERAS.values()):
            image = batch[target][0]
            assert image.shape == (3,224,224)
            assert image[i,112,112] == 1
            assert not image[[j for j in range(3) if j!=i]].any()
            assert batch[target+'_mask'].tolist() == [True]
        assert not batch['observation.images.image0'][..., :56, :].any()
        assert not batch['observation.images.image1'][..., :, :56].any()
        torch.testing.assert_close(batch['observation.images.image2'][0,2],torch.ones(224,224))
        np.testing.assert_array_equal(qpos, 3.)
        assert all(not v.flags.writeable for v in images.values())
    finally: backend.close()


def test_state_prompt_pads_the_tail_and_divides_by_three_without_reordering_or_clipping(tmp_path, monkeypatch):
    backend = prepared(tmp_path, monkeypatch)
    prompts = []
    tokenizer = backend.tokenizer
    def capture(text, **kwargs):
        prompts.append(text)
        return tokenizer(text, **kwargs)
    backend.tokenizer = capture
    # Avoid floating boundary ambiguity by specifying stats exactly for this test.
    backend.state_mean.zero_()
    backend.state_std.fill_(1.-1e-6)
    qpos = np.array([-6.,-3.,0.,3.,6.,0.,1.5,0.,0.,0.,0.,0.,0.,-1.5], np.float32)
    try:
        backend.predict(qpos, frames(), '  pick_up\ncup  ')
        want = [-1,0,128,255,255,128,192,128,128,128,128,128,128,64] + [128]*18
        assert prompts == ['Task: pick up cup, State: '+' '.join(map(str,want))+';\nAction: ']
    finally: backend.close()


def test_contiguous_first14_actions_unnormalize_before_delta_anchor_and_affine(tmp_path, monkeypatch):
    backend = prepared(tmp_path, monkeypatch, affine=True)
    backend.policy.output[:] = torch.arange(32)
    original = backend.policy.output.clone()
    qpos = np.full(14, 3., np.float32)
    try:
        output = backend.predict(qpos, frames(), 'lift2')
        # raw [0..13] -> * (2+eps) + 10; joints + 7; inverse affine (/2,-.5).
        want = np.array([8,9,10,11,12,13,10.5,15,16,17,18,19,20,17.5])
        np.testing.assert_allclose(output, np.tile(want,(3,1)), rtol=1e-6)
        torch.testing.assert_close(backend.policy.output, original)
        np.testing.assert_array_equal(qpos, 3.)
    finally: backend.close()


def test_gripper_delta_runs_once_after_robot_coordinates_and_before_fusion(tmp_path, monkeypatch):
    from rc_pi05_lift2.runtime import AsyncPi05Policy
    backend = prepared(tmp_path, monkeypatch, affine=True)
    # Runtime owns backend initialization, so give it an unloaded equivalent.
    backend.close()
    module = importlib.import_module('rc_pi05_lift2.backend')
    backend = module.Pi05Backend(backend.config)
    manifest = {'params':{'gripper_upper_threshold':4.5,'gripper_upper_value':'+0.5',
                          'gripper_lower_threshold':None}}
    policy = AsyncPi05Policy(backend.config,policy_config=manifest,backend=backend)
    runtime = SimpleNamespace(consumed_actions=0,control_epoch=1,config_version=1)
    try:
        output = policy.step({'qpos':np.full(14,3.,np.float32)},[],frames(),runtime=runtime)
        np.testing.assert_allclose(output[:,[6,13]],5.)
        np.testing.assert_allclose(output[:,:6],8.)
        runtime.consumed_actions = 1
        cached = policy.step({'qpos':np.full(14,99.)},output[1:],frames(),runtime=runtime)
        np.testing.assert_allclose(cached[:,[6,13]],5.)
        np.testing.assert_array_equal(policy.canonical,cached)
    finally: policy.close()


@pytest.mark.parametrize('bad', ['shape','nan','overflow'])
def test_invalid_model_result_cannot_become_robot_actions(tmp_path, monkeypatch, bad):
    backend = prepared(tmp_path, monkeypatch)
    if bad == 'shape': backend.policy.output = torch.zeros(1,5,16)
    elif bad == 'nan': backend.policy.output[0,0,31] = float('nan')
    else: backend.policy.output.fill_(torch.finfo(torch.float32).max)
    try:
        with pytest.raises(ValueError, match='finite|shape'):
            backend.predict(np.ones(14,np.float32),frames(),'task')
    finally: backend.close()


@pytest.mark.parametrize('bad', ['qpos','image','state_overflow'])
def test_invalid_observation_fails_before_forward(tmp_path, monkeypatch, bad):
    backend = prepared(tmp_path, monkeypatch)
    qpos, images = np.ones(14,np.float32), frames()
    if bad == 'qpos': qpos[0] = np.nan
    elif bad == 'image': images['head'] = np.ones((3,3,3),np.float32)
    else: qpos.fill(np.finfo(np.float32).max); backend.scale.fill(2.)
    try:
        with pytest.raises(ValueError): backend.predict(qpos,images,'task')
        assert backend.policy.batch is None
    finally: backend.close()
