import time

import pytest
import numpy as np
from rc_policy_sdk.images import ImagePool

from robot_control.global_state import GlobalStateService
from robot_control.process import PolicyProcess
from test_host_transport import Host, wait_for


SOURCE = '''
import time
def initialize(config, *, runtime):
    g = runtime.globals
    g['initialized'] = g.get('initialized', 0) + 1
    return {'g': g, 'config': config}
def policy(context, state, action_chunk, images, config, *, runtime):
    runtime.globals['inference'] = True
    return [state['qpos']]
def reset_context(context): context['g']['reset'] = True
def release(context):
    if context:
        time.sleep(context['config']['params'].get('release_delay',0))
        context['g']['released'] = True
def write(context):
    context['g']['command'] = 'ok'
    return context['g']['initialized']
'''


@pytest.mark.parametrize('release_delay', [0, 1.3])
def test_initialize_inference_reset_and_release_can_commit(tmp_path, release_delay):
    directory = tmp_path / 'policy' / 'a'
    directory.mkdir(parents=True)
    (directory / 'main.py').write_text(SOURCE)
    (directory / 'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
    service = GlobalStateService(tmp_path / 'global.env', tmp_path / 'ipc', run_id='run')
    service.start()
    host = Host(directory.parent, 'a')
    pool = ImagePool(cameras=('head',))
    pool.publish('head', np.zeros((2, 2), dtype=np.uint8), 1.)
    lease = pool.acquire()
    worker = None
    try:
        config = dict(schema_version=3, id='a', params={'release_delay': release_delay}, execution=dict(mode='sync', call_every_actions=1, control_mode='qpos'), hot_reloadable=[],
                      tui_commands={'write': dict(handler='write', args=[], timeout_s=2)})
        worker = PolicyProcess(directory, config, host_socket=host.socket, global_state=service,
                               init_timeout_s=5, release_timeout_s=2)
        worker.start()
        assert service.proxy['initialized'] == 1
        worker.submit_command('write-1', 'write', [])
        response = wait_for(worker.poll_command)
        if response['type'] == 'command_started':
            response = wait_for(worker.poll_command)
        assert response['type'] == 'command_result' and response['result'] == 1
        assert service.proxy['command'] == 'ok'
        worker.submit(dict(request_id=1, generation=1, config_version=1, state={'qpos': [0]*14}, action_chunk=[], images=lease.descriptors))
        response = wait_for(worker.poll)
        assert response['type'] == 'result', response
        worker.reset_context()
        assert service.proxy['reset'] and service.proxy['inference']
        assert not worker.close()
        assert service.proxy['released']
        assert worker.cleanup_confirmed
    finally:
        if worker is not None:
            worker.close()
        host.close()
        lease.release()
        pool.close()
        service.close(time.monotonic() + 3)


def test_proxy_requires_negotiated_capability():
    from rc_policy_sdk.runtime import PolicyRuntime
    with pytest.raises(RuntimeError, match='global'):
        PolicyRuntime(object()).globals


def test_native_chain_duplicate_members_share_one_store(tmp_path):
    import json
    from host_helpers import LocalPolicyProcess
    from robot_control.chain_process import ChainProcess
    from robot_control.config import PolicyRegistry
    for identity in ('a', 'b'):
        folder = tmp_path / 'policy' / identity
        folder.mkdir(parents=True)
        (folder / 'main.py').write_text(SOURCE)
        (folder / 'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
        (folder / 'policy.yaml').write_text(json.dumps(dict(schema_version=3, params={},
            execution=dict(mode='sync', call_every_actions=1, control_mode='qpos'))))
    chain = tmp_path / 'policy' / 'chain'
    chain.mkdir()
    (chain / 'policy.yaml').write_text(json.dumps(dict(schema_version=3, kind='chain', policies=['a','b','a'])))
    service = GlobalStateService(tmp_path / 'global.env', tmp_path / 'ipc', run_id='run')
    service.start()
    worker = ChainProcess(PolicyRegistry(tmp_path / 'policy').get('chain'),
                          process_factory=LocalPolicyProcess, global_state=service)
    try:
        worker.start()
        assert len(worker.monitor_members()) == 3
        assert service.proxy['initialized'] == 3
        assert not worker.close()
        assert service.proxy['released']
    finally:
        worker.close()
        service.close(time.monotonic() + 3)
