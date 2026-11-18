"""ACOne's strict opening threshold, applied once to each new prediction."""
import numpy as np

GRIPPER_PARAMS = ()


def gripper_rules(params):
    return {}


def process_grippers(actions):
    result = actions.copy()
    grippers = result[:, [6, 13]]
    result[:, [6, 13]] = np.where(grippers > np.float32(-1.7), np.float32(.04), grippers)
    return result
