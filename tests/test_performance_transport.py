"""Real host/SDK/self-chain diagnostics preserve results and image releases."""
import pytest

from rc_policy_sdk.performance import Trace
from test_native_chain_process import setup_chain, frames, wait_for


@pytest.mark.parametrize('enabled', [False, True])
def test_optional_timings_follow_each_real_chain_instance(setup_chain, frames, enabled):
    worker, children, _ = setup_chain()
    if enabled:
        Trace.start('run:infer:1').attach(frames)
    try:
        worker.start()
        worker.submit(frames)
        response = wait_for(worker.poll)
        assert response['type'] == 'result' and response['images_released'] is True
        assert response['actions'] == [[11.] * 14]
        if not enabled:
            assert '_performance' not in response
        else:
            assert '_performance' in response, 'worker did not return optional performance metadata'
            data = response['_performance']
            assert data['trace_id'] == 'run:infer:1'
            stages = {p['stage'] for p in data['points']}
            assert {'host.dispatch_received','runtime.dispatch_received','worker.dequeued',
                    'worker.policy','worker.normalize','rc.result_available','chain.child_collected'} <= stages
            assert {p['instance_id'] for p in data['points'] if p['stage']=='chain.child_collected'} == {'a-0','b','a-1'}
            assert worker.observe({**frames,'request_id':2})
            observed=wait_for(worker.poll_observation)
            assert '_performance' in observed
            assert 'worker.observe' in {p['stage'] for p in observed['_performance']['points']}
    finally:
        assert worker.close() is False


def test_failed_callback_keeps_diagnostic_path_and_release_proof(setup_chain, frames):
    worker, _, _ = setup_chain(fail='b')
    Trace.start('run:error:1').attach(frames)
    try:
        worker.start();worker.submit(frames)
        response=wait_for(worker.poll)
        assert response['type']=='error'
        assert 'worker.error' in {p['stage'] for p in response['_performance']['points']}
        assert 'images_released' not in response  # Aggregate failure is not a reader proof.
    finally:
        worker.close()
