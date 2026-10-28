"""End-to-end native chain result and image lease ordering with real workers."""
import json

import pytest

from test_chain_controller import native_controller as native_controller, operation
from test_controller import until


@pytest.mark.parametrize('waiting', [False, True])
@pytest.mark.parametrize('fail_later', [False, True])
def test_chain_report_waits_for_last_cell_and_real_release_proof(native_controller, waiting, fail_later):
    controller, children, root = native_controller
    events, gate = root / 'calls', root / 'allow-finish'
    source = f'''
import json, time
from pathlib import Path
def initialize(config): return {{'calls': 0}}
def policy(context, state, action_chunk, images, config, *, runtime):
    context['calls'] += 1
    with open({str(events)!r}, 'a') as stream:
        stream.write(json.dumps([config['id'], context['calls']]) + '\\n')
    if config['id'] == 'a': runtime.finish('chain result', outcome='terminated')
    if config['id'] == 'b':
        while not Path({str(gate)!r}).exists():
            assert not images['head'].flags.writeable
            time.sleep(.005)
        if {fail_later!r}: raise RuntimeError('later stage failed')
    return [[.9] * 14]
def reset_context(context): context['calls'] = 0
def release(context): pass
def test(context): return context['calls']
def change(context, value): return {{'params.value': value}}
'''
    for identity in ('a', 'b'):
        (root / 'policy' / identity / 'main.py').write_text(source)
    token = operation(controller, controller.manage_policy('open'))['result']['token']
    operation(controller, controller.manage_policy('apply', ['a', 'b', 'a'], token=token))
    worker = controller._worker
    if waiting:
        operation(controller, controller.submit('take', origin='mcp'))
        identity = controller.submit_policy_wait()
    else:
        operation(controller, controller.submit('resume'))
    try:
        until(lambda: events.exists() and '["b", 1]' in events.read_text())
        with controller._lock:
            lease = controller._inflight['lease']
            assert not lease._released
            assert controller.status()['control_owner'] == 'policy'
            if waiting:
                assert not controller.get_policy_result(identity)['finished']
        gate.touch()
        if fail_later:
            until(lambda: worker.cleanup_confirmed, 8)
            if waiting:
                assert controller.get_policy_result(identity)['status'] == 'failed'
                assert 'later stage failed' in controller.get_policy_result(identity)['reason']
        else:
            until(lambda: controller.status()['state'] == 'paused')
            assert [json.loads(line) for line in events.read_text().splitlines()] == [['a', 1], ['b', 1], ['a', 2]]
            assert len([child for child in children if child.alive]) == 2
            if waiting:
                result = controller.get_policy_result(identity)
                assert result['status'] == 'succeeded'
                assert result['result']['source'] == {'policy_id': 'a', 'cell': 1}
        until(lambda: lease._released)
        assert controller.status()['consumed'] == 0
        assert controller.status()['queue_remaining'] == 0
        assert controller.status()['control_owner'] == ('mcp' if waiting else 'policy')
    finally:
        gate.touch()
