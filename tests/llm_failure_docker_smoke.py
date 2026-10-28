"""Actual dynamic containers and installed packages, with a local HTTP model.

Build policy/llm_failure/Dockerfile (runtime), docker/rc.Dockerfile (test), and
policy/hold/Dockerfile using the image names below. No live provider or hardware.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

RC_IMAGE = 'robot-control-compose-policy-result-tests:local'
PRODUCER_IMAGE = 'robot-control-compose-policy-result-worker:local'
FAILURE_IMAGE = 'robot-control-compose-llm-failure:local'
EVIDENCE = Path('/private/tmp/rc-llm-failure-20261028')


def wait(predicate, timeout=40):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('llm_failure Docker condition timed out')
        time.sleep(.02)


def container():
    from llm_http_helpers import model_server, response_for
    from robot_control.controller import RobotController
    from robot_control.mcp_server import MCPService
    root = Path('/workspace')
    with model_server(host='0.0.0.0') as server:
        path = root / 'policy/llm_failure/policy.json'
        config = json.loads(path.read_text())
        config['params']['task'] = 'put the block in the tray'
        config['params']['provider'].update(base_url=f'http://robot-control:{server.server_port}/v1',
                                             api_key='local-test-key', timeout_s=15.)
        path.write_text(json.dumps(config))
        gate = threading.Event()
        server.responses.extend([
            {'wait': gate, 'body': response_for('deepseek', {'failed': False})},
            {'body': response_for('deepseek', {'failed': True})},
        ])
        controller = RobotController(root / 'configs/rc.mock.json')
        service = MCPService(controller, {'port': 0}, allow_ephemeral=True)
        outcomes = []

        def command(line):
            identity = controller.submit(line)
            wait(lambda: controller.get_operation(identity)['status'] in ('succeeded', 'failed', 'cancelled'))
            result = controller.get_operation(identity)
            assert result['status'] == 'succeeded', result
            return result.get('result')

        def tool(name, **arguments):
            return service.submit_tool(name, arguments).result(40)

        controller.start()
        try:
            wait(lambda: controller.status()['state'] in ('paused', 'fault'))
            assert controller.status()['state'] == 'paused', controller.read_events()
            worker = controller._worker
            members = worker.monitor_members()
            assert set(members) == {'producer', 'llm_failure'}
            assert members['producer'].host.instance_id != members['llm_failure'].host.instance_id
            service.start()
            assert not server.requests
            assert not tool('take').is_error
            receipt = tool('return_and_wait', timeout_s=0).structured_content
            identity = receipt['operation_id']
            assert not receipt['finished']
            assert server.received.wait(12)
            before = controller.status()['consumed']
            assert before >= 60
            wait(lambda: controller.status()['consumed'] > before + 75)
            assert len(server.requests) == 1
            assert controller.get_policy_result(identity)['status'] == 'running'
            first_body = server.requests[0]['body']
            assert first_body['thinking'] == {'type': 'disabled'}
            assert first_body['response_format'] == {'type': 'json_object'}
            assert json.dumps(first_body).count('data:image/jpeg;base64,') == 6
            gate.set()
            wait(lambda: controller.get_policy_result(identity)['finished'])
            result = tool('get_policy_result', operation_id=identity).structured_content
            assert result['status'] == 'succeeded' and result['result']['outcome'] == 'terminated'
            assert result['result']['source'] == {'policy_id': 'llm_failure', 'cell': 2}
            assert result['controller']['control_owner'] == 'mcp'
            assert len(server.requests) == 2
            def steps(body):
                return [int(value) for value in re.findall(r'executed_step=(\d+)', json.dumps(body))]
            assert steps(server.requests[1]['body'])[-1] - steps(first_body)[-1] >= 60
            assert json.dumps(server.requests[1]['body']).count('data:image/jpeg;base64,') == 9
            outcomes.append('60 consumed steps; async HTTP allows 75 more steps; no backlog; latest/history images; MCP termination')

            server.responses.append({'body': response_for('deepseek', {'failed': True})})
            command('resume')
            wait(lambda: controller.status()['state'] == 'paused')
            assert controller.status()['control_owner'] == 'policy'
            assert len(server.requests) == 3
            assert len(steps(server.requests[-1]['body'])) == 2, 'reset must remove old history'
            assert controller._worker is worker and worker.runnable
            outcomes.append('no wait only pauses; reset establishes fresh history; both workers retained')

            server.responses.append({'body': response_for('deepseek', {'failed': 'invalid'})})
            assert not tool('take').is_error
            result = tool('return_and_wait').structured_content
            assert result['status'] == 'failed' and '契约' in result['reason']
            assert result['controller']['control_owner'] == 'mcp'
            wait(lambda: controller._worker is None and controller._operation is None)
            assert not controller.status()['in_flight']
            outcomes.append('invalid JSON value produces policy fault, returns MCP and cleans complete runtime group')
        finally:
            gate.set()
            service.close()
            controller.submit('exit')
            assert controller.done.wait(30)
        assert controller.exit_code == 0
        report = {'rc_python': list(sys.version_info[:2]), 'outcomes': outcomes,
                  'model_requests': len(server.requests), 'exit_code': controller.exit_code}
        (root / 'outputs/result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)


def host():
    from project_templates import copy_templates
    root = Path(__file__).resolve().parents[1]
    token = uuid.uuid4().hex[:12]
    project = 'rc-llm-failure-' + token
    EVIDENCE.mkdir(exist_ok=True)
    endpoint = json.loads(subprocess.check_output(['docker', 'context', 'inspect']))[0]['Endpoints']['docker']['Host'][7:]
    endpoint = str(Path(endpoint).resolve())
    gid = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'stat',
        '--mount', f'type=bind,source={endpoint},target=/socket', PRODUCER_IMAGE, '-c', '%g', '/socket'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix='rc-llm-failure-', dir='/private/tmp') as temporary:
        directory = Path(temporary)
        for folder in ('configs', '.setup', 'outputs', 'snapshot', 'policy'):
            (directory / folder).mkdir()
        context = dict(version=3, deployment_id=token, host_root=temporary, client_root='/workspace',
                       base_project=project, rpc_volume=project+'-rpc', uid=os.getuid(), gid=os.getgid(),
                       docker_socket=endpoint, docker_gid=int(gid))
        (directory / '.setup/runtime.json').write_text(json.dumps(context))
        config = json.loads((root / 'configs/rc.mock.json.example').read_text())
        config.update(startup_policy='checked', frequency_hz=100,
                      timeouts=dict(initialize_s=30, inference_s=10, release_s=3, shutdown_s=25))
        config['safety']['enabled'] = False
        (directory / 'configs/rc.mock.json').write_text(json.dumps(config))
        producer = directory / 'policy/producer'
        producer.mkdir()
        (producer / 'main.py').write_text('''
import sys
def initialize(config):
    assert sys.version_info[:2] == (3, 14)
    return None
def policy(context, state, action_chunk, images, config): return [state['qpos']] * 180
def reset_context(context): pass
def release(context): pass
''')
        (producer / 'policy.json').write_text(json.dumps(dict(schema_version=3,
            execution=dict(mode='async', call_every_actions=1, control_mode='qpos'), params={})))
        (producer / 'compose.yaml').write_text(json.dumps({'services': {'policy': {'image': PRODUCER_IMAGE}}}))
        failure = directory / 'policy/llm_failure'
        copy_templates(root / 'policy/llm_failure', failure)
        (failure / 'compose.yaml').write_text(json.dumps({'services': {'policy': {
            'image': FAILURE_IMAGE, 'networks': ['model'], 'environment': {'NO_PROXY': 'robot-control,localhost,127.0.0.1'}}},
            'networks': {'model': {'external': True, 'name': project + '_default'}}}))
        (directory / 'policy/checked').mkdir()
        (directory / 'policy/checked/policy.json').write_text(json.dumps(
            dict(schema_version=3, kind='chain', policies=['producer', 'llm_failure'])))
        override = {'services': {'robot-control': {'image': RC_IMAGE,
            'command': ['python3', '/validation/llm_failure_docker_smoke.py', '--container'],
            'volumes': [{'type': 'bind', 'source': str(root / 'tests'), 'target': '/validation', 'read_only': True}]}}}
        (directory / 'override.json').write_text(json.dumps(override))
        env = dict(os.environ, RC_BASE_PROJECT=project, RC_UID=str(os.getuid()), RC_GID=str(os.getgid()),
                   RC_RPC_VOLUME=context['rpc_volume'], RC_DOCKER_GID=gid, RC_DOCKER_SOCKET=endpoint, RC_MCP_PORT='0')
        compose = ['docker', 'compose', '--env-file', '/dev/null', '--project-directory', temporary, '-p', project,
                   '-f', str(root / 'compose.yaml'), '-f', str(directory / 'override.json')]
        try:
            subprocess.run(compose+['up', '-d', '--no-build'], env=env, check=True)
            cid = subprocess.check_output(compose+['ps', '-aq', 'robot-control'], env=env, text=True).strip()
            code = subprocess.check_output(['docker', 'wait', cid], timeout=240, text=True).strip()
            logs = subprocess.check_output(['docker', 'logs', cid], text=True, stderr=subprocess.STDOUT)
            (EVIDENCE / 'cross-container.log').write_text(logs)
            assert code == '0', logs[-8000:]
            report = json.loads((directory / 'outputs/result.json').read_text())
            assert len(report['outcomes']) == 3 and report['exit_code'] == 0
            (EVIDENCE / 'cross-container.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            ids = subprocess.check_output(['docker', 'ps', '-aq', '--filter',
                'label=io.robot-control.deployment='+token], text=True).split()
            if ids:
                subprocess.run(['docker', 'rm', '-f', *ids], check=True)
            subprocess.run(compose+['down', '-v', '--remove-orphans'], env=env, check=True)


if __name__ == '__main__':
    container() if sys.argv[1:] == ['--container'] else host()
