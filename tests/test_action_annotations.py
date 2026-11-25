import numpy as np
import pytest

from rc_policy_sdk.actions import ActionChunk, normalize_actions, normalize_result
from rc_policy_sdk.transitions import TrajectoryAddTransition
from robot_control.buffer import ActionBuffer


def test_action_markers_follow_trim_protection_passthrough_and_confirmation():
    buffer = ActionBuffer()
    owner = ('example-0', 'context-one')
    values = np.arange(56, dtype=np.float32).reshape(4, 14)
    masks = np.zeros((4, 14), bool)
    masks[:, 0] = True
    request = buffer.begin_request()
    marked = ActionChunk(values, ('qpos',)*4, clipped=masks)
    assert buffer.accept(request, marked, owner=owner)
    assert buffer.clip_stats(owner)['clipped_actions'] == 0
    token = buffer.reserve()
    assert buffer.confirm(token)
    assert not buffer.confirm(token)
    request = buffer.begin_request()
    buffer.pin_current()
    # An unannotated passthrough keeps the markers of unchanged targets.
    assert buffer.accept(request, buffer.remaining())
    buffer.consume()
    assert buffer.clip_stats(owner) == {'clipped_actions': 2, 'clipped_dimensions': 2}
    buffer.invalidate()
    assert buffer.clip_stats(owner)['clipped_actions'] == 2
    assert buffer.clip_stats(('example-0', 'new-context'))['clipped_actions'] == 0


@pytest.mark.parametrize('masks', [np.ones((2, 14)), np.ones((1, 14), bool), np.ones((2, 12), bool)])
def test_invalid_marker_type_or_shape_rejected(masks):
    with pytest.raises(ValueError, match='clipped'):
        normalize_actions(ActionChunk(np.zeros((2, 14)), ('qpos',)*2, clipped=masks))


def test_discarded_prefix_and_replaced_predictions_never_contribute_to_counts():
    buffer = ActionBuffer()
    owner = ('example', 'context-one')
    buffer.install(np.zeros((4, 14)))
    request = buffer.begin_request()
    buffer.consume()
    buffer.consume()
    values = np.ones((5, 14), np.float32)
    markers = np.zeros(values.shape, bool)
    markers[:2] = True  # These stale rows must never be counted.
    markers[2:, :2] = True
    assert buffer.accept(request, ActionChunk(values, ('qpos',)*5, markers), owner=owner)
    assert buffer.last_trim == 2
    buffer.consume()
    assert buffer.clip_stats(owner) == {'clipped_actions': 1, 'clipped_dimensions': 2}
    assert buffer.accept(buffer.begin_request(), np.full((2, 14), 9.))
    buffer.consume()
    buffer.consume()
    assert buffer.clip_stats(owner) == {'clipped_actions': 1, 'clipped_dimensions': 2}
    # A late result from an invalidated request cannot restore markers.
    request = buffer.begin_request()
    buffer.invalidate()
    assert not buffer.accept(request, ActionChunk(values, ('qpos',)*5, markers), owner=owner)
    assert buffer.clip_stats(owner)['clipped_actions'] == 1


def test_action_annotations_roundtrip_is_owned_and_old_results_remain_valid():
    chunk = ActionChunk(np.zeros((2, 14)), ('qpos',)*2, np.ones((2, 14), bool))
    response = {**chunk.payload(), **chunk.annotations()}
    restored = normalize_result(response, 'qpos')
    response['action_annotations']['clipped'][0][0] = False
    assert restored.clipped.all() and not restored.clipped.flags.writeable
    assert normalize_result(chunk.payload(), 'qpos').clipped is None


@pytest.mark.parametrize('mode,resolved', [('close', [13, 13.1, 16.5]), ('tail', [13, 0, 16.5])])
def test_transition_starts_after_confirmed_reserved_and_pinned_actions(mode, resolved):
    old = np.repeat(np.arange(10, 16, dtype=np.float32)[:, None], 14, axis=1)
    new = np.repeat(np.array([9., 13.1, 16.5], dtype=np.float32)[:, None], 14, axis=1)
    buffer = ActionBuffer()
    buffer.install(old)
    request = buffer.begin_request()
    assert buffer.confirm(buffer.reserve())
    reserved = buffer.reserve()
    buffer.pin_current()
    transition = TrajectoryAddTransition(trunk_mode=mode, exponential_factor=5.0)
    response = ActionChunk(new, ('qpos',) * len(new), transition=transition).result_fields()
    assert buffer.accept(request, normalize_result(response, 'qpos'))
    assert buffer.last_trim == 3
    assert buffer.confirm(reserved)
    np.testing.assert_array_equal(buffer.current(), old[2])
    assert buffer.confirm(buffer.reserve())
    np.testing.assert_array_equal(buffer.current(), old[3])
    if mode == 'tail':
        alpha = (1 - np.exp(-1.25 / 2)) / (1 - np.exp(-1.25))
        resolved[1] = 14 + alpha * (13.1 - 14)
    np.testing.assert_allclose(buffer.remaining()[:, 0], resolved)


@pytest.mark.parametrize('mode', ['close', 'tail'])
def test_one_replaceable_action_starts_on_old_target(mode):
    old = np.full((2, 14), 0.5, np.float32)
    new = np.full((3, 14), 0.25, np.float32)
    buffer = ActionBuffer()
    buffer.install(old)
    request = buffer.begin_request()
    buffer.consume()
    transition = TrajectoryAddTransition(trunk_mode=mode, exponential_factor=5.0)
    assert buffer.accept(request, ActionChunk(new, ('qpos',) * len(new), transition=transition))
    np.testing.assert_array_equal(buffer.remaining(), np.concatenate((old[1:], new)))


def test_exhausted_old_trajectory_trims_new_actions_in_request_time():
    old = np.full((2, 14), 0.5, np.float32)
    new = np.repeat(np.arange(5, dtype=np.float32)[:, None], 14, axis=1)
    buffer = ActionBuffer()
    buffer.install(old)
    request = buffer.begin_request()
    buffer.consume()
    buffer.confirm(buffer.reserve())
    transition = TrajectoryAddTransition(trunk_mode='tail', exponential_factor=5.0)
    assert buffer.accept(request, ActionChunk(new, ('qpos',) * len(new), transition=transition))
    np.testing.assert_array_equal(buffer.remaining(), new[2:])
