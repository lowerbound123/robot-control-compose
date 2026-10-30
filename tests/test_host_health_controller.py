import numpy as np
from test_controller import setup_controller, ready, until


def test_failed_host_discards_cached_actions_without_waiting_for_next_request(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    controller.submit("resume")
    until(lambda: len(worker.requests) == 1)
    worker.finish(np.ones((4, 14)))
    until(lambda: controller.status()["queue_remaining"] > 0)
    # The host dies while the controller is running and no next request is
    # submitted; the control loop itself must drop the cached actions and fault.
    worker.runnable = False
    until(lambda: controller.status()["state"] == "fault")
    assert controller.status()["queue_remaining"] == 0
