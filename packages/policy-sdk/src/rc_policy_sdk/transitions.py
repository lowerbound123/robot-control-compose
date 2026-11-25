"""NumPy async-add algorithm migrated from deploydev/runtime/transitions.py.
Source commit: 74dc1f9a163c61a255c6f7aa4bc91d84054733f7.
See docs/model-server-policy.md for provenance and license.
"""
import numpy as np


def _closest_pair(old, new):
    distances = np.linalg.norm(old[:, None, :] - new[None, :, :], axis=2)
    # Row-major argmin breaks ties by earliest old index, then earliest new index.
    return np.unravel_index(distances.argmin(), distances.shape)


class TemporalTransition:
    """Blend aligned arm futures while preserving the old gripper plan.

    The first command remains on the already executing trajectory, while the
    final command in the window is exactly the new model trajectory. With an
    uncapped window, every real step shared by both pending futures is blended.
    Grippers are semantic open/close sequences whose event often occurs late in
    a chunk. Indices 6 and 13 therefore keep the pending old commands throughout
    the real overlap; the new gripper plan takes over after that overlap ends.
    """

    _GRIPPER_INDICES = [6, 13]

    def __init__(
        self,
        mode: str,
        window_steps: int | None,
        exponential_factor: float,
        exponential_smoothing: float = 0.25,
    ):
        if mode not in {"linear", "exponential"}:
            raise ValueError(f"unsupported temporal transition mode: {mode}")
        if window_steps is not None and window_steps <= 0:
            raise ValueError("transition.window_steps must be positive")
        if exponential_factor <= 0:
            raise ValueError("transition.exponential_factor must be positive")
        if exponential_smoothing <= 0:
            raise ValueError("transition.exponential_smoothing must be positive")
        self.mode = mode
        # None means use every real overlapping step. A numeric value remains
        # available for experiments that intentionally cap the blend window.
        self.window_steps = None if window_steps is None else int(window_steps)
        self.exponential_factor = float(exponential_factor)
        self.exponential_smoothing = float(exponential_smoothing)

    def apply(self, old_actions: np.ndarray, new_actions: np.ndarray) -> np.ndarray:
        old = np.asarray(old_actions, dtype=np.float32)
        new = np.asarray(new_actions, dtype=np.float32)
        overlap = min(len(old), len(new))
        if self.window_steps is not None:
            overlap = min(overlap, self.window_steps)
        if overlap <= 0:
            return new.copy()

        result = new.copy()
        alpha = self._weights(overlap).reshape(-1, 1)
        result[:overlap] = old[:overlap] + alpha * (new[:overlap] - old[:overlap])
        if result.ndim == 2 and result.shape[1] == 14:
            result[:overlap, self._GRIPPER_INDICES] = old[
                :overlap, self._GRIPPER_INDICES
            ]
        return result

    def _weights(self, length: int) -> np.ndarray:
        if length == 1:
            return np.ones(1, dtype=np.float32)
        progress = np.linspace(0.0, 1.0, num=length, dtype=np.float32)
        if self.mode == "linear":
            return progress
        # Match the reference TemporalInterpolator's early hand-off and gentle
        # convergence. The previous positive-exponent curve did the opposite:
        # it stayed on the old chunk, then made a large jump near the last step.
        decay = self.exponential_smoothing * self.exponential_factor
        weights = 1.0 - np.exp(-decay * progress)
        denominator = weights[-1]
        if denominator <= 0:
            return progress
        return (weights / denominator).astype(np.float32)


class ClosestPointOverlapTransition(TemporalTransition):
    """Align the nearest pair and crossfade all 14 axes over the shared interval."""

    def __init__(self):
        super().__init__("exponential", None, 5.0, 0.25)

    def apply(self, old_actions: np.ndarray, new_actions: np.ndarray) -> np.ndarray:
        # Distances and interpolation must also work for large finite float32 actions.
        old = np.asarray(old_actions, dtype=np.float64)
        new = np.asarray(new_actions, dtype=np.float64)
        if old.shape[1:] != (14,) or new.shape[1:] != (14,):
            raise ValueError("closest-point alignment requires two (steps, 14) arrays")
        if not len(old) or not len(new):
            return new.astype(np.float32)

        old_match, new_match = _closest_pair(old, new)
        before = min(old_match, new_match)
        old_start, new_start = old_match - before, new_match - before
        overlap = min(len(old) - old_start, len(new) - new_start)
        old_window = old[old_start:old_start + overlap]
        new_window = new[new_start:new_start + overlap]
        alpha = self._weights(overlap).astype(np.float64)[:, None]
        alpha[-1] = 1.0  # Land exactly on the new target, including a one-step overlap.
        blended = (1.0 - alpha) * old_window + alpha * new_window
        return np.concatenate((old[:old_start], blended, new[new_start + overlap:])).astype(np.float32)


class TrajectoryAddTransition(TemporalTransition):
    """Join async-add trajectories using one configured keypoint policy."""

    mode = "async-add"
    _TRUNK_MODES = frozenset({"tail", "close", "now"})
    SETTINGS = ('trunk_mode', 'trunk_smoothing', 'max_smoothing_steps',
                'exponential_factor', 'exponential_smoothing')

    def __init__(
        self,
        exponential_factor: float,
        exponential_smoothing: float = 0.25,
        trunk_mode: str = "tail",
        trunk_smoothing: bool = True,
        max_smoothing_steps: int = -1,
    ):
        super().__init__(
            "exponential",
            window_steps=None,
            exponential_factor=exponential_factor,
            exponential_smoothing=exponential_smoothing,
        )
        self.mode = "async-add"
        normalized_trunk_mode = str(trunk_mode).strip().lower()
        if normalized_trunk_mode not in self._TRUNK_MODES:
            raise ValueError(
                "transition.trunk_mode must be tail, close, or now; "
                f"got {trunk_mode!r}"
            )
        self.trunk_mode = normalized_trunk_mode
        if not isinstance(trunk_smoothing, bool):
            raise ValueError("transition.trunk_smoothing must be a boolean")
        self.trunk_smoothing = trunk_smoothing
        if (
            isinstance(max_smoothing_steps, bool)
            or not isinstance(max_smoothing_steps, int)
            or max_smoothing_steps == 0
            or max_smoothing_steps < -1
        ):
            raise ValueError(
                "transition.max_smoothing_steps must be -1 or a positive integer"
            )
        self.max_smoothing_steps = max_smoothing_steps

    def settings(self):
        return {name: getattr(self, name) for name in self.SETTINGS}

    def apply(self, old_actions: np.ndarray, new_actions: np.ndarray, *, preserve_first=False) -> np.ndarray:
        old = np.asarray(old_actions, dtype=np.float32)
        new = np.asarray(new_actions, dtype=np.float32)
        if (
            old.ndim != 2
            or new.ndim != 2
            or old.shape[1:] != new.shape[1:]
            or old.shape[1] != 14
        ):
            raise ValueError(
                "async-add trajectories must be aligned 2D action arrays; "
                f"got old={old.shape}, new={new.shape}"
            )
        if len(new) == 0:
            return new.copy()
        if len(old) == 0:
            return new.copy()

        applied_trunk_mode = self.trunk_mode
        if self.trunk_mode == "now" and len(old) < 3:
            applied_trunk_mode = "tail"

        if applied_trunk_mode == "now":
            old_keypoint = 2
            distances = np.linalg.norm(new - old[old_keypoint], axis=1)
            keypoint = int(np.argmin(distances))
        elif applied_trunk_mode == "close":
            old_keypoint, keypoint = _closest_pair(old, new)
        else:
            old_keypoint = len(old) - 1
            distances = np.linalg.norm(new - old[old_keypoint], axis=1)
            keypoint = int(np.argmin(distances))

        smoothing_applied = self.trunk_smoothing and self.trunk_mode != "now"
        if not smoothing_applied:
            result = np.concatenate(
                (old[:old_keypoint], new[keypoint:]), axis=0
            ).astype(np.float32, copy=False)
            return result

        blend_steps = min(old_keypoint + 1, keypoint + 1)
        if self.max_smoothing_steps != -1:
            blend_steps = min(blend_steps, self.max_smoothing_steps)
        if preserve_first and old_keypoint == 0 and not np.array_equal(old[0], new[keypoint]):
            # There is no earlier old command in this window. Keep the first
            # unsent target as the start of the handoff even with one overlap.
            return np.concatenate((old[:1], new[keypoint:]), axis=0)
        old_prefix = old[: old_keypoint + 1 - blend_steps]
        old_tail = old[old_keypoint + 1 - blend_steps : old_keypoint + 1]
        new_tail = new[keypoint + 1 - blend_steps : keypoint + 1]
        alpha = self._weights(blend_steps).reshape(-1, 1)
        blended = old_tail + alpha * (new_tail - old_tail)
        result = np.concatenate(
            (old_prefix, blended, new[keypoint + 1 :]), axis=0
        ).astype(np.float32, copy=False)
        return result
