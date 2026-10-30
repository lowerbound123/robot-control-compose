import pytest

from test_native_chain_process import SOURCE, frames as frames, rows, setup_chain as setup_chain, wait_for


def configure(worker, *, pause=False):
    source = SOURCE.replace("    event(config, 'policy')", """
    runtime.finish(config['id'] + ' result', outcome='terminated')
    if config['id'] == 'b' and config['params'].get('pause'):
        runtime.pause('operator')
    event(config, 'policy')""")
    for member in worker.record.members:
        member.config['params']['pause'] = pause
        (member.directory / 'main.py').write_text(source)


@pytest.mark.parametrize('pause', [False, True])
def test_native_chain_returns_first_finish_or_pause_after_all_cells(setup_chain, frames, pause):
    worker, children, events = setup_chain()
    configure(worker, pause=pause)
    try:
        worker.start()
        worker.submit(frames)
        result = wait_for(worker.poll)
        assert result['type'] == 'result', result
        assert result['actions'] == [[11.] * 14]
        assert [row[0] for row in rows(events) if row[1] == 'policy'] == ['a-0', 'b', 'a-1']
        assert len(children) == 3
        expected = ({'type': 'pause', 'reason': 'operator', 'source': {'policy_id': 'b', 'cell': 2}} if pause else
                    {'type': 'finish', 'outcome': 'terminated', 'message': 'a-0 result', 'source': {'policy_id': 'a-0', 'cell': 1}})
        assert result['control_requests'] == [expected]
        assert result['images_released'] is True
    finally:
        worker.close()


@pytest.mark.parametrize('failure', ['model', 'timeout', 'cancel'])
def test_finish_does_not_mask_later_chain_failure(setup_chain, frames, failure):
    worker, _, events = setup_chain(delays=(0, .2 if failure != 'model' else 0),
                                    timeout=.1 if failure == 'timeout' else 3,
                                    fail='b' if failure == 'model' else None)
    configure(worker)
    try:
        worker.start()
        worker.submit(frames)
        wait_for(lambda: ['b', 'policy'] in rows(events))
        if failure == 'cancel':
            worker.cancel_inference()
        response = wait_for(worker.poll)
        assert response['type'] == ('cancelled' if failure == 'cancel' else 'error')
        assert not response.get('control_requests')
    finally:
        worker.close()
