"""Local interpreter integration: deferred pause across native chain workers."""
import pytest

from test_native_chain_process import (
    SOURCE, frames as frames, rows, setup_chain as setup_chain, wait_for,
)


PAUSE_SOURCE = SOURCE.replace("    event(config, 'policy')", """
    if context['calls'] in config['params'].get('pause_calls', []):
        runtime.pause(config['id'] + ' finished')
        event(config, 'pause-requested')
    event(config, 'policy')""")


def enable_pause(worker, calls):
    for member in worker.record.members:
        member.config['params']['pause_calls'] = calls.get(member.config['id'], [])
        (member.directory / 'main.py').write_text(PAUSE_SOURCE)


@pytest.mark.parametrize('calls,source', [
    ({'A': [1, 2], 'B': [1]}, {'policy_id': 'A', 'cell': 1}),
    ({'B': [1]}, {'policy_id': 'B', 'cell': 2}),
    ({'A': [2]}, {'policy_id': 'A', 'cell': 3}),
])
def test_native_chain_finishes_all_cells_and_keeps_first_pause_source(setup_chain, frames, calls, source):
    worker, children, events = setup_chain()
    enable_pause(worker, calls)
    try:
        worker.start()
        worker.submit(frames)
        result = wait_for(worker.poll)
        assert result['type'] == 'result', result
        assert result['actions'] == [[12.] * 14]
        assert result['control_requests'] == [{'type': 'pause', 'reason': source['policy_id'] + ' finished',
                                               'source': source}]
        assert [row[0] for row in rows(events) if row[1] == 'policy'] == ['A', 'B', 'A']
        assert len(children) == 2 and all(child.alive for child in children)
        worker.submit({**frames, 'request_id': 2})
        result = wait_for(worker.poll)
        assert result['actions'] == [[34.] * 14], 'duplicate A keeps its context across both calls'
        assert not result.get('control_requests'), 'pause must not leak to the next inference'
    finally:
        worker.close()
    assert [row[0] for row in rows(events) if row[1] == 'release'] == ['B', 'A']


@pytest.mark.parametrize('failure', ['model', 'timeout', 'cancel'])
def test_pending_pause_never_masks_failure_or_cancellation(setup_chain, frames, failure):
    worker, _, events = setup_chain(delays=(0, .2 if failure != 'model' else 0),
                                    timeout=.1 if failure == 'timeout' else 3,
                                    fail='B' if failure == 'model' else None)
    enable_pause(worker, {'A': [1]})
    try:
        worker.start()
        worker.submit(frames)
        wait_for(lambda: ['B', 'policy'] in rows(events))
        if failure == 'cancel':
            worker.cancel_inference()
        result = wait_for(worker.poll)
        assert ['A', 'pause-requested'] in rows(events)
        assert result['type'] == ('cancelled' if failure == 'cancel' else 'error')
        assert not result.get('control_requests')
        assert rows(events).count(['A', 'policy']) == 1
    finally:
        worker.close()


def test_stale_child_pause_cannot_reach_chain_result(setup_chain, frames, monkeypatch):
    worker, children, _ = setup_chain()
    try:
        worker.start()
        child = children[0]
        poll = child.poll
        stale = {'type': 'result', 'images_released': True, 'request_id': 999, 'generation': frames['generation'],
                 'config_version': child.config_version, 'actions': [[999.] * 14],
                 'control_requests': [{'type': 'pause', 'reason': 'stale'}]}
        responses = [stale]
        monkeypatch.setattr(child, 'poll', lambda: responses.pop() if responses else poll())
        worker.submit(frames)
        result = wait_for(worker.poll)
        assert result['actions'] == [[12.] * 14]
        assert not result.get('control_requests')
    finally:
        worker.close()
