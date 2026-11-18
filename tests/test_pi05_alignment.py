"""Exercise the asynchronous handoff through both Pi05 policy entry points."""
import numpy as np

from robot_control.buffer import ActionBuffer
from test_pi05_task_controls import running, step


def test_global_closest_pair_uses_only_remaining_actions_and_all_14_columns(running):
    _, _, _, policy, model = running
    model.output[:, 1] = np.arange(50)
    chunk = step(policy, [], 0)

    model.output[:, 1] = 100 + np.arange(50)
    model.output[:6, 1] = [23, 24, 60, 61, 25, 29.25]
    model.output[4, 6] = 5  # Exact arm match at 25, but the left gripper differs.
    old = step(policy, chunk[21:], 21)
    policy.pending.result(2)
    result = step(policy, old[2:], 23)

    # New rows 0 and 1 matched exactly but expired during inference. Of the
    # remaining pairs, old 29 and new 29.25 are closest (neither is a tail).
    assert policy.last_trim == 2
    assert result.shape == (51, 14)
    np.testing.assert_array_equal(result[:4, 1], [23, 24, 25, 26])
    assert 25 < result[5, 1] < 28
    assert 0 < result[5, 6] < 5
    assert 29 < result[6, 1] < 29.25  # The matched pair is inside the window.
    assert 30 < result[7, 1] < model.output[6, 1]
    np.testing.assert_array_equal(result[26:, 1], model.output[25:, 1])
    np.testing.assert_array_equal(step(policy, result[1:], 24), result[1:])


def test_short_splice_rejected_by_rc_keeps_the_actual_pending_plan(running):
    _, _, _, policy, model = running
    model.output[:, 1] = np.arange(50)
    buffer = ActionBuffer()
    buffer.install(step(policy, [], 0))
    for _ in range(21):
        buffer.consume()

    model.output[:, 1] = 100
    model.output[-1, 1] = 21.25  # Old head is closest to the very last new action.
    step(policy, buffer.remaining(), buffer.consumed)
    policy.pending.result(2)
    model.gate.clear()  # Hold the next prediction while RC reports its retained plan.
    token = buffer.begin_request()
    buffer.pin_current()
    candidate = step(policy, buffer.remaining(), buffer.consumed)
    assert candidate.shape == (1, 14)
    assert candidate[0, 1] == 21.25
    assert not buffer.accept(token, candidate)
    retained = buffer.remaining()
    np.testing.assert_array_equal(step(policy, retained, buffer.consumed), retained)


def test_both_sides_blend_all_14_columns_until_the_new_segment_ends(running):
    _, _, _, policy, _ = running
    old = np.tile(np.array([1, 2, 10, 13, 20, 24, 27], np.float32)[:, None], (1, 14))
    new = np.tile(np.array([6, 8, 10, 16, 19], np.float32)[:, None], (1, 14))
    result = policy.transition.apply(old, new)
    # Unique match at (2, 2); new ends first. Check the signed contribution
    # from every joint and gripper on both sides, including exact endpoints.
    assert result.shape == new.shape
    np.testing.assert_array_equal(result[[0, 2, 4]], [old[0], new[2], new[4]])
    assert np.all((old[1] < result[1]) & (result[1] < new[1]))
    assert np.all((old[3] < result[3]) & (result[3] < new[3]))
