import time
from robot_control.recording import RecordingService


def test_late_publication_keeps_original_attempt_id(tmp_path):
    service = RecordingService(tmp_path, 'late', lambda: {})
    events = []
    service.event = lambda channel, event, **kwargs: events.append(event)
    service._state = 'recording'
    service._boundary = 0
    first = service.control_attempt({'requested': [1] * 14})
    second = service.control_attempt({'requested': [2] * 14})
    service.sent({'arm': 'left', 'mode': 'qpos', 'values7': [1] * 7, 'attempt_id': first,
                  'timestamp_ns': time.time_ns(), 'monotonic_ns': time.monotonic_ns()})
    assert first != second
    assert events[-1]['attempt_id'] == first
