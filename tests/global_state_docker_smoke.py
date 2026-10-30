"""Mounted-source RC 3.10 + workers 3.11/3.14; task-owned mock deployment only."""
from config_helpers import read_config
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from source_helpers import copy_runtime_sources, source_report, anchor_override

RC_IMAGE = os.environ.get('RC_TEST_RC_IMAGE', 'robot-control-compose-global-tests:local')
IMAGES = {'a': os.environ.get('RC_TEST_WORKER314_IMAGE', 'robot-control-compose-global-worker314:local'),
          'b': os.environ.get('RC_TEST_WORKER311_IMAGE', 'robot-control-compose-global-worker311:local')}
SOURCE = '''
import sys,time
def initialize(config,*,runtime):
    g=runtime.globals
    assert g['baseline']==17
    # This fixture maps its known instance names to identifier-only global keys.
    key=config['id'].replace('-','_')+'_initialized'
    g[key]=g.get(key,0)+1
    return {'g':g,'id':config['id'],'key':config['id'].replace('-','_')}
def policy(context,state,action_chunk,images,config,*,runtime):
    assert images and all(not x.flags.writeable for x in images.values())
    key=context['key']+'_inferences'
    runtime.globals[key]=runtime.globals.get(key,0)+1
    return action_chunk if len(action_chunk) else [state['qpos']]
def reset_context(context): context['g'][context['key']+'_reset']=True
def release(context):
    if context:
        time.sleep(1.2)
        key=context['key']+'_released'
        context['g'][key]=context['g'].get(key,0)+1
def info(context):
    version,data=context['g'].snapshot()
    return {'python':list(sys.version_info[:2]),'version':version,'data':data}
def write(context):
    context['g'][context['key']+'_message']={'text':'跨容器 hello','ok':True}
    return context['g'].snapshot()[0]
'''


def wait(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('container acceptance timed out')
        time.sleep(.02)


def container():
    from robot_control.controller import RobotController
    from robot_control.cli import shutdown_signals
    from robot_control.global_state_file import read_state
    from robot_control.mcp_server import MCPService
    assert sys.version_info[:2] == (3, 10)
    import rc_policy_sdk
    assert rc_policy_sdk.__file__.startswith('/workspace/packages/policy-sdk/src/')
    import robot_control
    assert robot_control.__file__.startswith('/workspace/src/')
    root = Path('/workspace')
    config = root / 'configs/rc.mock.yaml'
    outcomes = []
    controller = RobotController(config)

    def command(line):
        identity = controller.submit(line)
        wait(lambda: controller.get_operation(identity)['status'] in ('succeeded', 'failed', 'cancelled'))
        result = controller.get_operation(identity)
        assert result['status'] == 'succeeded', (result, controller.read_events())
        return result['result']

    controller.start()
    try:
        wait(lambda: controller.status()['state'] in ('paused', 'fault'))
        assert controller.status()['state'] == 'paused', controller.read_events()
        members = controller._worker.monitor_members()
        assert list(members) == ['a-0', 'b', 'a-1']
        assert len({worker.host.instance_id for worker in members.values()}) == 3
        assert command('policy a-0 info')['python'] == [3, 14]
        assert command('policy a-1 info')['python'] == [3, 14]
        assert command('policy b info')['python'] == [3, 11]
        assert all(controller.globals[name+'_initialized'] == 1 for name in ('a_0', 'b', 'a_1'))
        command('set global.rc_message "hello world"')
        command('policy a-0 write')
        data = command('policy b info')['data']
        assert data['rc_message'] == 'hello world' and data['a_0_message']['ok']
        assert command('policy a-1 info')['data']['a_0_message']['text'] == '跨容器 hello'
        command('policy b write')
        assert command('policy a-0 info')['data']['b_message']['text'] == '跨容器 hello'
        outcomes.append('source 3.10 RC / 3.14 a-0,a-1 / 3.11 b: three independent initializations and mutually visible globals')
        command('resume')
        wait(lambda: all(controller.globals.get(name+'_inferences', 0) > 0 for name in ('a_0', 'b', 'a_1')))
        command('pause')
        assert all(controller.globals[name+'_reset'] for name in ('a_0', 'b', 'a_1'))
        command('set global.removed null')
        command('unset global.removed')
        assert 'removed' not in controller.globals
        old = {key: worker.endpoint.container_id for key, worker in members.items()}
        command('switch pair')
        members = controller._worker.monitor_members()
        assert all(worker.endpoint.container_id != old[key] for key, worker in members.items())
        assert all(controller.globals[name+'_initialized'] == 2 for name in ('a_0', 'b', 'a_1'))
        assert all(controller.globals[name+'_released'] == 1 for name in ('a_0', 'b', 'a_1'))
        outcomes.append('a-0/b/a-1 separate inference/reset/release; three cold replacements preserve shared globals and slow release commits')
        service = MCPService(controller, {'port': 0}, allow_ephemeral=True)
        try:
            service.start()
            response = service.submit_tool('show', {'scope': 'global'}).result(10)
            assert not response.is_error
            response = service.submit_tool('command', {'line': 'set global.denied true', 'wait': True}).result(10)
            assert response.is_error and 'denied' not in controller.globals
            assert not service.submit_tool('take', {}).result(10).is_error
            assert not service.submit_tool('command', {'line': 'set global.mcp true', 'wait': True}).result(10).is_error
        finally:
            service.close()
    finally:
        controller.submit('exit')
        assert controller.done.wait(35), controller.read_events()
    assert controller.exit_code == 0, controller.read_events()
    saved = read_state(root / 'configs/global.env', 1048576)
    assert all(saved[name+'_released'] == 2 for name in ('a_0', 'b', 'a_1'))
    assert saved['mcp'] and saved['baseline'] == 17
    outcomes.append('MCP read-only/take; exit file contains all successful writes including final slow release')
    controller = RobotController(config)
    with shutdown_signals(controller):
        controller.start()
        try:
            wait(lambda: controller.status()['state'] in ('paused', 'fault'))
            assert all(controller.globals[name+'_initialized'] == 3 for name in ('a_0', 'b', 'a_1'))
            assert controller.globals['mcp']
            os.kill(os.getpid(), signal.SIGTERM)
            assert controller.done.wait(35)
        finally:
            if not controller.done.is_set():
                controller.submit('exit')
                assert controller.done.wait(35)
    assert controller.exit_code == 0, controller.read_events()
    saved = read_state(root / 'configs/global.env', 1048576)
    assert all(saved[name+'_released'] == 3 for name in ('a_0', 'b', 'a_1'))
    assert not list((root / 'outputs/runtime').glob('global-*.json'))
    outcomes.append('new RC loads persisted dict; real SIGTERM saves final release; resource journals reclaimed')
    report = dict(outcomes=outcomes, exit_code=0, rc_python=list(sys.version_info[:2]), sources=source_report())
    (root / 'outputs/global-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)


def host():
    root = Path(__file__).resolve().parents[1]
    evidence = Path(os.environ.get('RC_GLOBAL_EVIDENCE', '/private/tmp/rc-global-state-20261029'))
    evidence.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    project = 'rc-global-' + token
    endpoint = json.loads(subprocess.check_output(['docker', 'context', 'inspect']))[0]['Endpoints']['docker']['Host'][7:]
    endpoint = str(Path(endpoint).resolve())
    gid = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'stat',
        '--mount', f'type=bind,source={endpoint},target=/socket', IMAGES['a'], '-c', '%g', '/socket'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix='rc-global-', dir='/private/tmp') as temporary:
        directory = Path(temporary)
        copy_runtime_sources(directory)
        for folder in ('configs', '.setup', 'outputs', 'snapshot', 'policy'):
            (directory / folder).mkdir()
        context = dict(version=3, deployment_id=token, host_root=temporary, client_root='/workspace',
                       base_project=project, rpc_volume=project+'-rpc', uid=os.getuid(), gid=os.getgid(),
                       docker_socket=endpoint, docker_gid=int(gid))
        (directory / '.setup/runtime.json').write_text(json.dumps(context))
        config = read_config(root / 'configs/rc.mock.yaml.example')
        config.update(startup_policy='pair', frequency_hz=30,
                      timeouts=dict(initialize_s=35, inference_s=10, release_s=4, shutdown_s=30))
        (directory / 'configs/rc.mock.yaml').write_text(json.dumps(config))
        (directory / 'configs/global.env').write_text('baseline=17\n')
        for name in ('a', 'b'):
            folder = directory / 'policy' / name
            folder.mkdir()
            (folder / 'main.py').write_text(SOURCE)
            (folder / 'policy.yaml').write_text(json.dumps(dict(schema_version=3,
                execution=dict(mode='sync', call_every_actions=1, control_mode='qpos'), params={},
                tui_commands={key: dict(handler=key, args=[], timeout_s=3) for key in ('info', 'write')})))
            (folder / 'compose.yaml').write_text(json.dumps({'services': {'policy': {'image': IMAGES[name]}}}))
        (directory / 'policy/pair').mkdir()
        (directory / 'policy/pair/policy.yaml').write_text(json.dumps(dict(schema_version=3, kind='chain', policies=['a','b','a'])))
        override = {'services': {'robot-control': {'image': RC_IMAGE,
            'command': ['python3', '/validation/global_state_docker_smoke.py', '--container'],
            'volumes': [{'type': 'bind', 'source': str(root / 'tests'), 'target': '/validation', 'read_only': True}]}}}
        anchor_override(override['services'])
        (directory / 'override.json').write_text(json.dumps(override))
        env = dict(os.environ, RC_BASE_PROJECT=project, RC_UID=str(os.getuid()), RC_GID=str(os.getgid()),
                   RC_RPC_VOLUME=context['rpc_volume'], RC_DOCKER_GID=gid, RC_DOCKER_SOCKET=endpoint, RC_MCP_PORT='0')
        compose = ['docker', 'compose', '--env-file', '/dev/null', '--project-directory', temporary, '-p', project,
                   '-f', str(root / 'compose.yaml'), '-f', str(directory / 'override.json')]
        try:
            subprocess.run(compose + ['up', '-d', '--no-build'], env=env, check=True)
            cid = subprocess.check_output(compose + ['ps', '-aq', 'robot-control'], env=env, text=True).strip()
            code = subprocess.check_output(['docker', 'wait', cid], timeout=240, text=True).strip()
            logs = subprocess.check_output(['docker', 'logs', cid], text=True, stderr=subprocess.STDOUT)
            (evidence / 'cross-container.log').write_text(logs)
            assert code == '0', logs[-10000:]
            report = read_config(directory / 'outputs/global-result.json')
            (evidence / 'cross-container.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            ids = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'label=io.robot-control.deployment=' + token], text=True).split()
            if ids:
                subprocess.run(['docker', 'rm', '-f', *ids], check=True)
            subprocess.run(compose + ['down', '-v', '--remove-orphans'], env=env, check=True)


if __name__ == '__main__':
    container() if sys.argv[1:] == ['--container'] else host()
