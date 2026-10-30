"""Mounted-source RC 3.10 -> SDK 3.14 acceptance, using only task-owned mock containers.

Build robot-control-compose-policy-result-tests:local with docker/rc.Dockerfile
and robot-control-compose-policy-result-worker:local with policy/hold/Dockerfile.
Then run this script on the Docker host. No model or hardware service is used.
"""
from config_helpers import read_config
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from source_helpers import copy_runtime_sources, source_report, anchor_override


RC_IMAGE = os.environ.get('RC_TEST_RC_IMAGE', 'robot-control-compose-policy-result-tests:local')
WORKER_IMAGE = os.environ.get('RC_TEST_WORKER314_IMAGE', 'robot-control-compose-policy-result-worker:local')
WORKER = '''
import sys, time
from pathlib import Path
type Number = float
def initialize(config):
    assert sys.version_info[:2] == (3, 14)
    import rc_policy_sdk
    assert rc_policy_sdk.__file__.startswith('/workspace/packages/policy-sdk/src/')
    return {'calls': 0}
def policy(context, state, action_chunk, images, config, *, runtime):
    context['calls'] += 1
    assert images and all(not image.flags.writeable for image in images.values())
    if Path(__file__).resolve().parent.name == 'a':
        runtime.finish('container result')
    else:
        while not Path(__file__).with_name('allow').exists(): time.sleep(.01)
        if Path(__file__).with_name('fail').exists(): raise RuntimeError('container failure')
    return [state['qpos']]
def reset_context(context): context['calls'] = 0
def release(context): pass
def info(context): return {'calls': context['calls'], 'python': list(sys.version_info[:2])}
'''


def wait(predicate, timeout=35):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('acceptance condition timed out')
        time.sleep(.02)


def container():
    from robot_control.controller import RobotController
    from robot_control.mcp_server import MCPService
    import robot_control

    assert sys.version_info[:2] == (3, 10)
    import robot_control
    assert robot_control.__file__.startswith('/workspace/src/')
    import rc_policy_sdk
    assert rc_policy_sdk.__file__.startswith('/workspace/packages/policy-sdk/src/')
    root = Path('/workspace')
    gate = root / 'policy/b/allow'
    controller = RobotController(root / 'configs/rc.mock.yaml')
    service = MCPService(controller, {'port': 0}, allow_ephemeral=True)
    outcomes = []

    def command(line):
        identity = controller.submit(line)
        wait(lambda: controller.get_operation(identity)['status'] in ('succeeded', 'failed', 'cancelled'))
        result = controller.get_operation(identity)
        assert result['status'] == 'succeeded', result
        return result['result']

    def tool(name, **arguments):
        return service.submit_tool(name, arguments).result(35)

    def pending_chain():
        wait(lambda: controller.status()['in_flight'])
        wait(lambda: command('policy b info')['calls'] == 1)
        assert command('policy a-0 info')['calls'] == 1
        assert command('policy a-1 info')['calls'] == 0
        assert not controller._inflight['lease']._released

    controller.start()
    try:
        wait(lambda: controller.status()['state'] in ('paused', 'fault'))
        assert controller.status()['state'] == 'paused', controller.read_events()
        members = controller._worker.monitor_members()
        assert list(members) == ['a-0', 'b', 'a-1']
        assert len({worker.host.instance_id for worker in members.values()}) == 3
        assert command('policy a-0 info')['python'] == [3, 14]
        assert command('policy a-1 info')['python'] == [3, 14]
        service.start()
        assert not tool('take').is_error
        waiting = service.submit_tool('return_and_wait', {})
        pending_chain()
        assert not waiting.done()
        assert not tool('get_policy_result').structured_content['finished']
        gate.touch()
        result = waiting.result(35).structured_content
        assert result['result'] == {'outcome': 'completed', 'message': 'container result',
                                    'source': {'policy_id': 'a-0', 'cell': 1}}
        assert result['controller']['control_owner'] == 'mcp'
        assert command('policy a-0 info')['calls'] == 1
        assert command('policy a-1 info')['calls'] == 1
        assert controller.status()['consumed'] == 0
        outcomes.append('source Py3.10/Py3.14; a-0/b/a-1 uses three containers and independent counters; deferred first-instance result and readonly frames')

        gate.unlink()
        result = tool('return_and_wait', timeout_s=0).structured_content
        identity = result['operation_id']
        assert result['timed_out'] and not result['finished']
        pending_chain()
        gate.touch()
        wait(lambda: controller.get_policy_result(identity)['finished'])
        for _ in range(2):
            queried = tool('get_policy_result', operation_id=identity).structured_content
            assert queried['status'] == 'succeeded' and queried['controller']['control_owner'] == 'mcp'
        outcomes.append('timeout retains handback; repeated query is nonconsuming')

        gate.unlink()
        waiting = service.submit_tool('return_and_wait', {})
        pending_chain()
        lease = controller._inflight['lease']
        command('pause')
        result = waiting.result(35).structured_content
        assert result['status'] == 'cancelled' and result['reason'] == '人工接管'
        assert controller.status()['control_owner'] == 'policy' and not lease._released
        gate.touch()
        wait(lambda: not controller.status()['in_flight'])
        assert lease._released
        command('resume')
        wait(lambda: controller.status()['state'] == 'paused')
        assert controller.status()['control_owner'] == 'policy'
        outcomes.append('manual pause cancels episode; old lease waits for ACK; no wait preserves policy ownership')

        (root / 'policy/b/fail').touch()
        assert not tool('take').is_error
        result = tool('return_and_wait').structured_content
        assert result['status'] == 'failed' and 'container failure' in result['reason']
        assert result['controller']['control_owner'] == 'mcp'
        wait(lambda: controller._worker is None and controller._operation is None)
        assert not controller.status()['in_flight']
        outcomes.append('later member failure defeats finish; MCP receives failure; runtime and leases cleaned')
    finally:
        gate.touch()
        service.close()
        controller.submit('exit')
        assert controller.done.wait(30)
    assert controller.exit_code == 0
    report = {'rc_python': list(sys.version_info[:2]), 'sources': source_report(),
              'outcomes': outcomes, 'exit_code': controller.exit_code}
    (root / 'outputs/result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)


def host():
    root = Path(__file__).resolve().parents[1]
    token = uuid.uuid4().hex[:12]
    project = 'rc-policy-result-' + token
    evidence = Path(os.environ.get('RC_RESULT_EVIDENCE', '/private/tmp/rc-policy-result-20261028'))
    evidence.mkdir(parents=True, exist_ok=True)
    endpoint = json.loads(subprocess.check_output(['docker', 'context', 'inspect']))[0]['Endpoints']['docker']['Host'][7:]
    endpoint = str(Path(endpoint).resolve())
    gid = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'stat',
        '--mount', f'type=bind,source={endpoint},target=/socket', WORKER_IMAGE, '-c', '%g', '/socket'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix='rc-policy-result-', dir='/private/tmp') as temporary:
        directory = Path(temporary)
        copy_runtime_sources(directory)
        for folder in ('configs', '.setup', 'outputs', 'snapshot', 'policy'):
            (directory / folder).mkdir()
        context = dict(version=3, deployment_id=token, host_root=temporary, client_root='/workspace',
                       base_project=project, rpc_volume=project+'-rpc', uid=os.getuid(), gid=os.getgid(),
                       docker_socket=endpoint, docker_gid=int(gid))
        (directory / '.setup/runtime.json').write_text(json.dumps(context))
        config = read_config(root / 'configs/rc.mock.yaml.example')
        config.update(startup_policy='pair', frequency_hz=50,
                      timeouts=dict(initialize_s=30, inference_s=30, release_s=3, shutdown_s=25))
        (directory / 'configs/rc.mock.yaml').write_text(json.dumps(config))
        for name in ('a', 'b'):
            folder = directory / 'policy' / name
            folder.mkdir()
            (folder / 'main.py').write_text(WORKER)
            (folder / 'policy.yaml').write_text(json.dumps(dict(schema_version=3,
                execution=dict(mode='sync', call_every_actions=1, control_mode='qpos'), params={},
                tui_commands={'info': dict(handler='info', args=[], timeout_s=3)})))
            (folder / 'compose.yaml').write_text(json.dumps({'services': {'policy': {'image': WORKER_IMAGE}}}))
        (directory / 'policy/pair').mkdir()
        (directory / 'policy/pair/policy.yaml').write_text(json.dumps(
            dict(schema_version=3, kind='chain', policies=['a', 'b', 'a'])))
        override = {'services': {'robot-control': {'image': RC_IMAGE,
            'command': ['python3', '/validation/policy_result_docker_smoke.py', '--container'],
            'volumes': [{'type': 'bind', 'source': str(root / 'tests'), 'target': '/validation', 'read_only': True}]}}}
        anchor_override(override['services'])
        (directory / 'override.json').write_text(json.dumps(override))
        env = dict(os.environ, RC_BASE_PROJECT=project, RC_UID=str(os.getuid()), RC_GID=str(os.getgid()),
                   RC_RPC_VOLUME=context['rpc_volume'], RC_DOCKER_GID=gid, RC_DOCKER_SOCKET=endpoint, RC_MCP_PORT='0')
        compose = ['docker', 'compose', '--env-file', '/dev/null', '--project-directory', temporary, '-p', project,
                   '-f', str(root / 'compose.yaml'), '-f', str(directory / 'override.json')]
        try:
            subprocess.run(compose+['up', '-d', '--no-build'], env=env, check=True)
            cid = subprocess.check_output(compose+['ps', '-aq', 'robot-control'], env=env, text=True).strip()
            code = subprocess.check_output(['docker', 'wait', cid], timeout=180, text=True).strip()
            logs = subprocess.check_output(['docker', 'logs', cid], text=True, stderr=subprocess.STDOUT)
            (evidence / 'cross-container.log').write_text(logs)
            assert code == '0', logs[-8000:]
            report = read_config(directory / 'outputs/result.json')
            assert len(report['outcomes']) == 4
            (evidence / 'cross-container.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            ids = subprocess.check_output(['docker', 'ps', '-aq', '--filter',
                'label=io.robot-control.deployment='+token], text=True).split()
            if ids:
                subprocess.run(['docker', 'rm', '-f', *ids], check=True)
            subprocess.run(compose+['down', '-v', '--remove-orphans'], env=env, check=True)


if __name__ == '__main__':
    container() if sys.argv[1:] == ['--container'] else host()
