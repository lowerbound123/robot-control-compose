"""Shared, GPU-free checkpoint validation for preflight and inference."""
import json
from pathlib import Path

import numpy as np

# Bundled source schemas with two six-joint arms and the three input RGB keys.
# Keep preflight independent of the model framework imported by schema classes.
SUPPORTED_SCHEMAS = frozenset({
    'arx_acone', 'arx_lift2', 'piper', 'split_aloha',
    'ARX Lift-2', 'AgileX Split Aloha', 'ARX AC One',
})


def read_action_stats(checkpoint, stats_key):
    """Read either source flat or split layout, requiring finite 14-wide stats."""
    try:
        with (Path(checkpoint) / 'stats.json').open(encoding='utf-8') as stream:
            stats = json.load(stream)[stats_key]
        keys = ('min', 'max', 'mean', 'std')
        if 'observation.state' in stats and 'action' in stats:
            state = {key: np.asarray(stats['observation.state'][key]) for key in keys}
            action = {key: np.asarray(stats['action'][key]) for key in keys}
        else:
            def assemble(prefix):
                return {key: np.concatenate([
                    stats[f'{prefix}.{name}.position'][key]
                    for name in ('left_joint', 'left_gripper', 'right_joint', 'right_gripper')
                ]) for key in keys}
            state, action = assemble('states'), assemble('actions')
        for values in (state, action):
            for value in values.values():
                if value.shape != (14,) or not np.isfinite(value).all():
                    raise ValueError('stats must contain finite 14-dimensional arrays')
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'invalid stats.json entry {stats_key!r}: {exc}') from exc
    return {'observation.state': state}, {'action': action}
