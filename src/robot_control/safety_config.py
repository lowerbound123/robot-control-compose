"""Connection-owned limit settings; stdlib-only so setup can validate them."""
import copy
import math


DEFAULT_SAFETY = {
    'enabled': True,
    'reference_frequency_hz': 50,
    'max_delta_per_step': [1000000.] * 14,
    'epos': {'translation_m_s': 1000000., 'rotation_rad_s': 1000000.,
             'gripper_units_s': 1000000.},
}
_FLOAT32_MAX = 3.4028234663852886e38


def safety_settings(config, *, fallback=None):
    """Copy RC defaults and overlay the device's fields, including epos leaves."""
    if not isinstance(config, dict) or set(config) - set(DEFAULT_SAFETY):
        raise ValueError('safety must be an object containing known limit settings')
    values = copy.deepcopy(DEFAULT_SAFETY) if fallback is None else safety_settings(fallback)
    epos = config.get('epos', {})
    if not isinstance(epos, dict) or set(epos) - set(DEFAULT_SAFETY['epos']):
        raise ValueError('safety.epos must contain known physical rates')
    values.update(copy.deepcopy({key: value for key, value in config.items() if key != 'epos'}))
    values['epos'].update(copy.deepcopy(epos))
    if type(values['enabled']) is not bool:
        raise ValueError('safety.enabled must be a boolean')
    reference = values['reference_frequency_hz']
    if type(reference) not in (int, float) or not math.isfinite(reference) or reference <= 0:
        raise ValueError('safety.reference_frequency_hz must be finite and positive')
    limits = values['max_delta_per_step']
    if (not isinstance(limits, list) or len(limits) != 14 or
            any(type(value) not in (int, float) or not math.isfinite(value) or
                abs(value) > _FLOAT32_MAX or (value <= 0 and value != -1) for value in limits)):
        raise ValueError('safety.max_delta_per_step requires 14 finite float32 positive values or -1')
    for key, value in values['epos'].items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'safety.epos.{key} must be finite and positive')
    return values
