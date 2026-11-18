"""Assignment/delta rules for one fresh, unnormalized prediction."""
import math
import re

import numpy as np

SIGNED_DECIMAL = re.compile(r'[+-](?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
FLOAT32_MAX = float(np.finfo(np.float32).max)


def parse_gripper_value(value, name):
    delta = isinstance(value, str)
    if (delta and SIGNED_DECIMAL.fullmatch(value) is None or
            not delta and type(value) not in (int, float)):
        raise ValueError(f'{name} must be a finite number or signed decimal string')
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f'{name} must fit a finite float32 action') from None
    if not math.isfinite(number) or abs(number) > FLOAT32_MAX:
        raise ValueError(f'{name} must fit a finite float32 action')
    return delta, number


def _comparison_threshold(value):
    # NumPy 2 otherwise casts a weak Python scalar outside float32's range to
    # infinity. Promote only those bounds; ordinary equality stays float32.
    return np.float64(value) if abs(value) > FLOAT32_MAX else value


def process_grippers(actions, *, upper_threshold, upper_value, lower_threshold, lower_value):
    result = actions.copy()
    grippers = result[:, [6, 13]]
    # Retain the model's comparison precision at inclusive threshold boundaries.
    upper = grippers >= _comparison_threshold(upper_threshold)
    lower = (~upper & (grippers <= _comparison_threshold(lower_threshold)) if lower_threshold is not None
             else np.zeros_like(upper))
    # Float64 permits checking the sum before the result is stored as float32.
    original = grippers.astype(np.float64)
    processed = original.copy()
    for mask, value, name in ((upper, upper_value, 'params.gripper_upper_value'),
                              (lower, lower_value, 'params.gripper_lower_value')):
        delta, number = parse_gripper_value(value, name)
        # Evaluate only the winning rule: an unused offset may otherwise overflow.
        if mask.any():
            selected = original[mask] + number if delta else number
            if not np.isfinite(selected).all() or np.any(np.abs(selected) > FLOAT32_MAX):
                raise ValueError(f'{name} result must fit a finite float32 action')
            processed[mask] = selected
    result[:, [6, 13]] = processed
    return result


GRIPPER_PARAMS = ('gripper_upper_threshold', 'gripper_upper_value',
                  'gripper_lower_threshold', 'gripper_lower_value')


def gripper_rules(params):
    rules = {key.removeprefix('gripper_'): params[key] for key in GRIPPER_PARAMS}
    for key, value in rules.items():
        if key.endswith('_value'):
            parse_gripper_value(value, 'params.gripper_' + key)
        elif key == 'lower_threshold' and value is None:
            continue
        elif type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f'params.gripper_{key} must be a finite number')
    return rules
