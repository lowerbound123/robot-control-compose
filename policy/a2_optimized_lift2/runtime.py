"""A2 actions in native LIFT2 joint and gripper coordinates."""
from rc_qwen35_history.a2.runtime import OptimizedPolicy as BaseOptimizedPolicy
from rc_qwen35_history.gripper import process_grippers


class OptimizedPolicy(BaseOptimizedPolicy):
    robot_type = 'arx_lift2'
    default_gripper_upper_threshold = 2.5

    @staticmethod
    def _process(actions, upper_threshold=2.5, *, upper_value=4.9, lower_threshold=2.5, lower_value=0.0):
        return process_grippers(actions, upper_threshold=upper_threshold, upper_value=upper_value,
                                lower_threshold=lower_threshold, lower_value=lower_value)
