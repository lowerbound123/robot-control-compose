import numpy as np
from test_controller import setup_controller, ready


def test_failed_host_discards_cached_actions_without_waiting_for_next_request(setup_controller):
    controller,_,_=setup_controller
    worker=ready(controller)
    with controller._lock:
        controller.buffer.install(np.ones((4,14)))
        controller._state='running'
        worker.runnable=False
        controller._tick()
        assert controller.status()['queue_remaining']==0
        assert controller.status()['state']=='fault'
