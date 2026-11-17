"""Native LeRobot CPU processors with synthetic forward output; no trained model."""
import importlib

import numpy as np
import pytest

torch = pytest.importorskip('torch')
pytest.importorskip('lerobot.policies.pi05.configuration_pi05')

from pi05_helpers import native_checkpoint_fixture, write_json
from test_pi05_backend import Predictor, frames


@pytest.mark.parametrize('normalization', ['MEAN_STD','QUANTILES'])
def test_lift2_uses_saved_processors_before_gripper_rules(tmp_path, monkeypatch, normalization):
    from rc_pi05_lift2.configuration import load_model_config
    from rc_pi05_lift2.runtime import AsyncPi05Policy
    from types import SimpleNamespace
    module = importlib.import_module('rc_pi05_lift2.backend')
    path, _, _ = native_checkpoint_fixture(tmp_path, monkeypatch, normalization=normalization)
    monkeypatch.setattr(module, 'load_local_policy', lambda checkpoint,cfg:Predictor(cfg))
    config = load_model_config(path, project_root=tmp_path)
    policy = AsyncPi05Policy(config, project_root=tmp_path)
    try:
        output = policy.step({'qpos':np.full(14,3.,np.float32)}, [], frames(),
            runtime=SimpleNamespace(consumed_actions=0,control_epoch=1,config_version=1))
        assert output.shape==(3,14)
        np.testing.assert_array_equal(policy.canonical,output)
        np.testing.assert_allclose(output[:,:6],10.)
        np.testing.assert_allclose(output[:,7:13],10.)
        np.testing.assert_allclose(output[:,[6,13]],4.9)
        assert 'observation.language.tokens' in policy.backend.policy.batch
    finally:
        policy.close()


@pytest.mark.parametrize('relative,delta', [(False,False),(False,True),(True,False)])
def test_lift2_affine_and_relative_coordinates_are_restored_once(tmp_path, monkeypatch, relative, delta):
    from rc_pi05_lift2.configuration import load_model_config
    module = importlib.import_module('rc_pi05_lift2.backend')
    path, raw, _ = native_checkpoint_fixture(tmp_path, monkeypatch, relative=relative)
    raw['observations'].update(state_scale=[2.]*14,state_offset=[1.]*14)
    if delta:raw['actions']['representation']='delta_joint'
    write_json(path,raw)
    monkeypatch.setattr(module,'load_local_policy',lambda checkpoint,cfg:Predictor(cfg))
    backend = module.Pi05Backend(load_model_config(path,project_root=tmp_path))
    try:
        backend.load()
        output = backend.predict(np.full(14,3.,np.float32),frames(),'lift2 test')
        joints=[0,1,2,3,4,5,7,8,9,10,11,12]
        np.testing.assert_allclose(output[:,joints],8. if relative or delta else 4.5)
        np.testing.assert_allclose(output[:,[6,13]],4.5)
    finally:
        backend.close()


@pytest.mark.parametrize('normalization', ['MEAN_STD','QUANTILES'])
def test_delta_applies_after_saved_processors_and_robot_unit_conversion(tmp_path, monkeypatch, normalization):
    from rc_pi05_lift2.configuration import load_model_config
    from rc_pi05_lift2.runtime import AsyncPi05Policy
    from types import SimpleNamespace
    module = importlib.import_module('rc_pi05_lift2.backend')
    path, raw, _ = native_checkpoint_fixture(tmp_path, monkeypatch, normalization=normalization)
    raw['observations'].update(state_scale=[2.]*14,state_offset=[1.]*14)
    write_json(path,raw)
    monkeypatch.setattr(module,'load_local_policy',lambda checkpoint,cfg:Predictor(cfg))
    config = load_model_config(path,project_root=tmp_path)
    manifest = {'params':{'gripper_upper_threshold':4.5,'gripper_upper_value':'+0.5',
                          'gripper_lower_threshold':None}}
    policy = AsyncPi05Policy(config,policy_config=manifest,project_root=tmp_path)
    try:
        output = policy.step({'qpos':np.full(14,3.,np.float32)},[],frames(),
            runtime=SimpleNamespace(consumed_actions=0,control_epoch=1,config_version=1))
        np.testing.assert_allclose(output[:,[6,13]],5.)
        np.testing.assert_allclose(output[:,:6],4.5)
        np.testing.assert_allclose(output[:,7:13],4.5)
    finally:
        policy.close()
