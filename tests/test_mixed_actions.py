import numpy as np
import pytest

from robot_control.buffer import ActionBuffer
from robot_control.config import validate_policy


def test_mixed_buffer_trims_modes_with_values():
    buffer = ActionBuffer()
    chunk = {"actions": np.arange(56).reshape(4, 14), "modes": ["qpos", "epos", "qpos", "epos"]}
    buffer.install(chunk)
    token = buffer.begin_request()
    buffer.consume()
    assert buffer.accept(token, chunk)
    assert buffer.current_mode() == "epos"
    remaining = buffer.remaining_payload("mixed")
    assert remaining["modes"] == ["epos", "qpos", "epos"]
    assert remaining["actions"][0] == list(range(14, 28))
    buffer.invalidate()
    assert buffer.remaining_payload("mixed") == {"actions": [], "modes": []}
    assert not buffer.accept(token, chunk)


def test_mode_validation_and_legacy_projection():
    from rc_policy_sdk.actions import normalize_actions

    for mode in ("qpos", "epos"):
        chunk = normalize_actions(np.zeros((2, 14)), mode)
        assert chunk.modes == (mode, mode)
        assert chunk.payload(mode) == [[0.0] * 14] * 2
    with pytest.raises(ValueError):
        normalize_actions(np.zeros((2, 14)), "mixed")
    for bad in (
        {"actions": [[0] * 14], "modes": []},
        {"actions": [[0] * 14], "modes": ["velocity"]},
        {"actions": [[float("nan")] * 14], "modes": ["qpos"]},
        {"actions": [], "modes": []},
        {"actions": [[0] * 13], "modes": ["epos"]},
    ):
        with pytest.raises(ValueError):
            normalize_actions(bad, "mixed")


def test_control_mode_is_cold_even_when_listed_hot():
    from test_config_buffer import manifest

    config = manifest()
    config["execution"]["control_mode"] = "mixed"
    validate_policy(config)
    config["hot_reloadable"].append("execution.control_mode")
    with pytest.raises(ValueError, match="control_mode"):
        validate_policy(config)


def test_epos_limiter_shortest_rotation_and_feedback_reanchor():
    from robot_control.safety import ModeLimiter

    cfg = {"enabled": True, "reference_frequency_hz": 50,
           "max_delta_per_step": [0.1] * 14,
           "epos": {"translation_m_s": 0.1, "rotation_rad_s": 1, "gripper_units_s": 1}}
    limiter = ModeLimiter(cfg, 10)
    qpos = np.zeros(14)
    epos = np.array([1, 0, 0, 0, 0, np.pi - .05, 0] * 2)
    state = {"qpos": qpos, "eef": epos}
    limiter.reset(qpos)
    goal = epos.copy()
    goal[[0, 7]] += .02
    goal[[5, 12]] = -np.pi + .05
    sent, done = limiter.apply(goal, "epos", state)
    assert not done
    np.testing.assert_allclose(sent[[0, 7]], [1.01, 1.01], atol=1e-6)
    assert np.all(np.abs(sent[[5, 12]]) > 3)
    changed_state = {"qpos": [.4] * 14, "eef": epos}
    sent, done = limiter.apply([.5] * 14, "qpos", changed_state)
    np.testing.assert_allclose(sent, .5)
    assert done
