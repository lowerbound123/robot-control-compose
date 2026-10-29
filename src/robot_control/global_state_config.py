"""Cold global-state settings; stdlib-only for setup validation."""
import math


def settings(value=None):
    result = dict(path='global.env', max_bytes=1048576, timeout_s=2.0)
    if value is not None:
        if type(value) is not dict or set(value) - result.keys():
            raise ValueError('invalid global_state settings')
        result.update(value)
    if not isinstance(result['path'], str) or not result['path']:
        raise ValueError('global_state.path must be a nonempty string')
    if type(result['max_bytes']) is not int or not 2 <= result['max_bytes'] <= 3145728:
        raise ValueError('global_state.max_bytes must be between 2 and 3145728')
    timeout = result['timeout_s']
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('global_state.timeout_s must be positive and finite')
    return result
