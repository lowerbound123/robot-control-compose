from pathlib import Path

import numpy as np
import pytest

from config_helpers import read_config
from rc_policy_sdk.transitions import TrajectoryAddTransition

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mode', ['close', 'tail', 'now'])
@pytest.mark.parametrize('smooth', [False, True])
def test_fusion_matches_frozen_source_golden(mode, smooth):
    fixture = read_config(ROOT / 'tests/fixtures/async_add_golden.json')
    result = TrajectoryAddTransition(trunk_mode=mode, trunk_smoothing=smooth,
                                     max_smoothing_steps=5, exponential_factor=5.,
                                     exponential_smoothing=.25).apply(
                                         np.array(fixture['old'], np.float32),
                                         np.array(fixture['new'], np.float32))
    # Golden was frozen from deploydev 74dc1f9; float32 blend order differs in the
    # last bit, so compare with a tolerance instead of exact equality.
    np.testing.assert_allclose(result, np.array(fixture[f'{mode}:{smooth}'], np.float32),
                               atol=1e-6)
