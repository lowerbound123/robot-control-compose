"""NumPy temporal blending adapted from deploydev/runtime/transitions.py.
Source commit: 74dc1f9a163c61a255c6f7aa4bc91d84054733f7.
"""
import numpy as np

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
