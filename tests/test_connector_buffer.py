import numpy as np
from robot_control.buffer import ActionBuffer


def test_submitted_targets_advance_without_consumption_until_receipt():
    buffer = ActionBuffer()
    buffer.install(np.array([[1.] * 14, [2.] * 14, [3.] * 14]))
    first = buffer.reserve()
    second = buffer.reserve()
    assert buffer.consumed == 0
    assert buffer.current()[0] == 3
    assert buffer.confirm(first)
    assert buffer.consumed == 1
    assert not buffer.confirm(first)
    assert buffer.confirm(second)
    assert buffer.consumed == 2


def test_async_result_cannot_replace_submitted_or_partially_sent_prefix():
    buffer = ActionBuffer()
    buffer.install(np.array([[1.] * 14, [2.] * 14, [3.] * 14]))
    request = buffer.begin_request()
    first = buffer.reserve()
    buffer.pin_current()
    assert buffer.accept(request, np.array([[11.] * 14, [12.] * 14, [13.] * 14, [14.] * 14]))
    assert buffer.remaining()[:, 0].tolist() == [1, 2, 13, 14]
    assert buffer.current()[0] == 2
    assert buffer.last_trim == 2
    assert buffer.confirm(first)
    assert buffer.consumed == 1
    assert buffer.remaining()[:, 0].tolist() == [2, 13, 14]


def test_take_invalidates_accounting_but_not_receipt_identity():
    buffer = ActionBuffer()
    buffer.install(np.ones((2, 14)))
    old = buffer.reserve()
    buffer.invalidate()
    buffer.install(np.ones((2, 14)) * 4)
    assert not buffer.confirm(old)
    assert buffer.consumed == 0 and buffer.current()[0] == 4
