import numpy as np
import pytest

from rc_policy_sdk.actions import ActionChunk, normalize_actions, normalize_result
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
