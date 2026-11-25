"""NumPy-only absolute action contract shared by the worker and controller."""
from dataclasses import dataclass
from collections.abc import Mapping

import numpy as np

from .transitions import TrajectoryAddTransition

CONTROL_MODES = ("qpos", "epos", "mixed")


@dataclass(frozen=True)
class ActionChunk:
    actions: np.ndarray
    modes: tuple[str, ...]
    clipped: np.ndarray | None = None
    transition: TrajectoryAddTransition | None = None

    def annotations(self):
        return {} if self.clipped is None else {'action_annotations': {'clipped': self.clipped.tolist()}}

    def result_fields(self):
        fields = {**self.payload(), **self.annotations()}
        if self.transition is not None:
            fields['action_transition'] = self.transition.settings()
        return fields

    def payload(self, control_mode="mixed"):
        if control_mode not in CONTROL_MODES:
            raise ValueError("unknown control_mode")
        values = self.actions.tolist()
        if control_mode == "mixed":
            return {"actions": values, "modes": list(self.modes)}
        if any(mode != control_mode for mode in self.modes):
            raise ValueError("action modes do not match policy control_mode")
        return values


def normalize_actions(value, control_mode=None, *, allow_empty=False):
    """Validate, copy and normalize arrays or explicitly tagged mixed chunks."""
    if control_mode is not None and control_mode not in CONTROL_MODES:
        raise ValueError("control_mode must be qpos, epos or mixed")
    clipped = transition = None
    if isinstance(value, ActionChunk):
        clipped, transition = value.clipped, value.transition
        value = value.payload()
    if isinstance(value, Mapping):
        if set(value) != {"actions", "modes"}:
            raise ValueError("mixed actions require exactly actions and modes")
        raw, modes = value["actions"], value["modes"]
        if not isinstance(modes, (list, tuple)) or any(mode not in ("qpos", "epos") for mode in modes):
            raise ValueError("modes must contain qpos or epos")
    else:
        if control_mode == "mixed":
            raise ValueError("mixed policy must return actions and modes")
        raw, modes = value, None
    try:
        with np.errstate(over="ignore", invalid="ignore"):
            array = np.array(raw, dtype=np.float32, copy=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("actions must be finite (T,14) numbers") from exc
    if allow_empty and array.shape == (0,):
        array = array.reshape(0, 14)
    if (array.ndim != 2 or array.shape[1] != 14 or not np.isfinite(array).all()
            or (len(array) == 0 and not allow_empty)):
        raise ValueError("actions must be finite nonempty (T,14) absolute targets")
    modes = tuple(modes) if modes is not None else (control_mode or "qpos",) * len(array)
    if len(modes) != len(array):
        raise ValueError("actions and modes must have equal lengths")
    if control_mode in ("qpos", "epos") and any(mode != control_mode for mode in modes):
        raise ValueError("action mode differs from policy control_mode")
    array.flags.writeable = False
    if clipped is not None:
        clipped = np.array(clipped, copy=True)
        if clipped.dtype != np.bool_ or clipped.shape != array.shape:
            raise ValueError('clipped markers must be boolean with the action shape')
        clipped.flags.writeable = False
    if transition is not None:
        if (not isinstance(transition, TrajectoryAddTransition) or transition.trunk_mode not in ('close', 'tail')
                or not len(array) or any(mode != 'qpos' for mode in modes) or clipped is not None):
            raise ValueError('action_transition requires nonempty qpos actions without clipped markers and close or tail mode')
    return ActionChunk(array, modes, clipped, transition)


def normalize_result(response, control_mode=None):
    payload = ({'actions': response['actions'], 'modes': response['modes']}
               if 'modes' in response else response['actions'])
    chunk = normalize_actions(payload, control_mode)
    annotations = response.get('action_annotations')
    transition = response.get('action_transition')
    if annotations is None and transition is None:
        return chunk
    if annotations is not None and (not isinstance(annotations, dict) or set(annotations) != {'clipped'}):
        raise ValueError('invalid action_annotations')
    if transition is not None:
        if (not isinstance(transition, dict) or set(transition) != set(TrajectoryAddTransition.SETTINGS)
                or any(type(transition[key]) not in (float, int) or not np.isfinite(transition[key])
                       for key in ('exponential_factor', 'exponential_smoothing'))):
            raise ValueError('invalid action_transition settings')
        transition = TrajectoryAddTransition(**transition)
    clipped = None if annotations is None else annotations['clipped']
    return normalize_actions(ActionChunk(chunk.actions, chunk.modes, clipped, transition), control_mode)
