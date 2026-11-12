"""MCP-compatible direction geometry producing the ordinary (T,14) contract."""
from dataclasses import dataclass
import warnings

import numpy as np
from scipy.spatial.transform import Rotation, Slerp


MOTION_AXES = {
    'forward': (0, 1), 'backward': (0, -1), 'left': (1, 1), 'right': (1, -1),
    'up': (2, 1), 'down': (2, -1), 'twist-clockwise': (3, 1),
    'twist-counterclockwise': (3, -1), 'tilt-down-toward-table': (4, 1),
    'tilt-up-away-from-table': (4, -1), 'turn-left-across-table': (5, 1),
    'turn-right-across-table': (5, -1),
}
ACTIONS = (*MOTION_AXES, 'open', 'close', 'noop')


@dataclass(frozen=True)
class Decision:
    left: str
    right: str
    done: bool


def parse_decision(value):
    if (not isinstance(value, dict) or set(value) != {'left', 'right', 'done'}
            or type(value['done']) is not bool
            or not isinstance(value['left'], str) or value['left'] not in ACTIONS
            or not isinstance(value['right'], str) or value['right'] not in ACTIONS):
        raise ValueError('decision must contain left/right action names and a boolean done')
    # Completion always discards the proposed movement, including inconsistent responses.
    return Decision('noop', 'noop', True) if value['done'] else Decision(**value)


def epos(value):
    with np.errstate(over='ignore', invalid='ignore'):
        result = np.array(value, dtype=np.float32, copy=True)
    if result.shape != (14,) or not np.isfinite(result).all():
        raise ValueError('eef must contain 14 finite absolute targets')
    return result


def make_chunk(baseline, decision, params):
    start = epos(baseline).astype(np.float64)
    size = params['chunk_size']
    fractions = np.arange(1, size + 1, dtype=float) / size
    rows = np.tile(start, (size, 1))
    for offset, action in ((0, decision.left), (7, decision.right)):
        if action in MOTION_AXES:
            axis, sign = MOTION_AXES[action]
            if axis < 3:
                rows[:, offset + axis] += fractions * sign * params['translation_m']
            else:
                delta = np.zeros(3)
                delta[axis - 3] = sign * params['rotation_rad']
                rotation = Rotation.from_euler('xyz', start[offset + 3:offset + 6])
                target = rotation * Rotation.from_rotvec(delta)
                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore', message='Gimbal lock detected.*', category=UserWarning)
                    rows[:, offset + 3:offset + 6] = Slerp(
                        [0, 1], Rotation.concatenate([rotation, target]))(fractions).as_euler('xyz')
        elif action in ('open', 'close'):
            goal = params['gripper_' + action]
            rows[:, offset + 6] += fractions * (goal - start[offset + 6])
        elif action != 'noop':
            raise ValueError('unknown direction')
    with np.errstate(over='ignore', invalid='ignore'):
        rows = rows.astype(np.float32)
    if not np.isfinite(rows).all():
        raise ValueError('generated actions exceed finite float32 range')
    return rows
