"""Shared finite, absolute dual-arm target validation."""
import numpy as np

ARMS = ('left', 'right')


def validate_qpos(value):
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (14,) or not np.isfinite(result).all():
        raise ValueError('qpos action must contain exactly 14 finite values')
    return result.copy()


def validate_arm_targets(targets):
    if not isinstance(targets, dict) or not targets or set(targets) - set(ARMS):
        raise ValueError('targets must select left and/or right')
    result = {}
    for arm, target in targets.items():
        if not isinstance(target, dict) or target.get('mode') not in ('qpos', 'epos'):
            raise ValueError('target mode must be qpos or epos')
        values = np.asarray(target.get('values'), dtype=float)
        if values.shape != (7,) or not np.isfinite(values).all():
            raise ValueError('arm target requires 7 finite values')
        result[arm] = {'mode': target['mode'], 'values': values.tolist()}
    return result


def arm_targets(values, mode='qpos'):
    values = validate_qpos(values)
    return validate_arm_targets({arm: {'mode': mode, 'values': values[i*7:i*7+7].tolist()}
                                 for i, arm in enumerate(ARMS)})
