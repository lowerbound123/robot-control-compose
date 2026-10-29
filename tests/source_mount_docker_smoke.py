"""Real RC restart / policy cold-switch source edits with unchanged image IDs.

Uses only a temporary deployment and a mock robot. Set RC_TEST_RC_IMAGE,
RC_TEST_ANCHOR_IMAGE and RC_TEST_WORKER314_IMAGE to freshly built dependency images.
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

from source_helpers import ROOT, copy_runtime_sources, source_report

RC_IMAGE = os.environ.get('RC_TEST_RC_IMAGE', 'robot-control-compose-dynamic-tests:local')
WORKER_IMAGE = os.environ.get('RC_TEST_WORKER314_IMAGE', 'robot-control-compose-policy:local')
ANCHOR_IMAGE = os.environ.get('RC_TEST_ANCHOR_IMAGE', 'robot-control-compose-anchor:local')
POLICY = '''
import source_probe
import rc_policy_sdk
from pathlib import Path
def initialize(config):
    for module in (source_probe, rc_policy_sdk):
        try:
            with Path(module.__file__).open('a') as f: f.write('')
        except OSError: pass
        else: raise AssertionError('source mount is writable')
    assert source_probe.__cached__.startswith('/tmp/rc-pycache/')
    return source_probe.VALUE
def policy(context,state,action_chunk,images,config): return [state['qpos']]
def reset_context(context): pass
def release(context): pass
def info(context): return context
'''


def wait(predicate, timeout=50):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('source reload acceptance timed out')
        time.sleep(.03)


def in_container():
    from robot_control.controller import RobotController
    root = Path('/workspace')
    phase = os.environ['SOURCE_TEST_PHASE']
    controller = RobotController(root / 'configs/rc.mock.yaml')
    assert controller.source_probe == phase

    def command(line, expected='succeeded'):
        identity = controller.submit(line)
        wait(lambda: controller.get_operation(identity)['status'] in ('succeeded', 'failed', 'cancelled'))
        result = controller.get_operation(identity)
        assert result['status'] == expected, (result, controller.read_events())
        return result.get('result')

    controller.start()
    report = {'rc_source': controller.source_probe, 'sources': source_report()}
    try:
        wait(lambda: controller.status()['state'] in ('paused', 'fault'))
        assert controller.status()['state'] == 'paused', controller.read_events()
        assert command('policy probe info') == ('first' if phase == 'first' else 'other')
        if phase == 'first':
            old = controller._worker.endpoint.container_id
            image = subprocess.check_output(['docker', 'inspect', '-f', '{{.Image}}', old], text=True).strip()
            (root / 'outputs/ready').touch()
            wait(lambda: (root / 'outputs/updated').exists())
            command('switch probe')
            new = controller._worker.endpoint.container_id
            assert old != new
            assert subprocess.check_output(['docker', 'inspect', '-f', '{{.Image}}', new], text=True).strip() == image
            assert command('policy probe info') == 'other'
            report['policy_reload_same_image'] = image
            (root / 'outputs/ready-missing').touch()
            wait(lambda: (root / 'outputs/missing').exists())
            command('switch probe', expected='failed')
            assert controller._worker is None
            report['missing_source_rejected'] = True
    finally:
        controller.submit('exit')
        assert controller.done.wait(35), controller.read_events()
    assert controller.exit_code == 0, controller.read_events()
    (root / f'outputs/{phase}.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


def no_source_audit(image):
    # No project source or source mounts. Dependency/test stages must both pass.
    code = '''
import importlib.util, importlib.metadata, json, sys
names = ('robot_control','rc_policy_sdk','rc_qwen35_policy','rc_pi05_policy',
         'rc_llm_failure','rc_llm_direction','robot_control_test_sim')
for name in names: assert importlib.util.find_spec(name) is None, name
for name in ('robot-control','rc-policy-sdk','rc-qwen35-policy','rc-pi05-policy',
             'rc-llm-failure','rc-llm-direction','rc-qwen35-vendor'):
    try: importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError: pass
    else: raise AssertionError(name)
print(json.dumps({'python':sys.version.split()[0], 'no_project_packages':True}))
'''
    return json.loads(subprocess.check_output(['docker', 'run', '--rm', '--network', 'none',
        '--entrypoint', 'python3', image, '-c', code], text=True))


def on_host():
    evidence = Path(os.environ['RC_SOURCE_EVIDENCE']) if 'RC_SOURCE_EVIDENCE' in os.environ else Path(
        tempfile.mkdtemp(prefix='rc-source-evidence-'))
    evidence.mkdir(parents=True, exist_ok=True)
    images = [RC_IMAGE, WORKER_IMAGE, ANCHOR_IMAGE]
    ids = {image: subprocess.check_output(['docker','image','inspect','-f','{{.Id}}',image], text=True).strip() for image in images}
    audit = {image: no_source_audit(image) for image in images}
    endpoint = json.loads(subprocess.check_output(['docker','context','inspect']))[0]['Endpoints']['docker']['Host'][7:]
    endpoint = str(Path(endpoint).resolve())
    gid = subprocess.check_output(['docker','run','--rm','--entrypoint','stat',
        '--mount',f'type=bind,source={endpoint},target=/socket',WORKER_IMAGE,'-c','%g','/socket'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix='rc-source-deployment-') as temporary:
        directory = Path(temporary)
        copy_runtime_sources(directory)
        for name in ('configs','.setup','outputs','snapshot','policy/probe'):
            (directory / name).mkdir(parents=True)
        token = uuid.uuid4().hex[:12]
        project = 'rc-source-' + token
        context = dict(version=3, deployment_id=token, host_root=str(directory), client_root='/workspace',
            base_project=project, rpc_volume=project+'-rpc', uid=os.getuid(), gid=os.getgid(),
            docker_socket=endpoint, docker_gid=int(gid))
        (directory / '.setup/runtime.json').write_text(json.dumps(context))
        config = read_config(ROOT / 'configs/rc.mock.yaml.example')
        config.update(startup_policy='probe', timeouts=dict(initialize_s=30,inference_s=5,release_s=3,shutdown_s=30))
        (directory / 'configs/rc.mock.yaml').write_text(json.dumps(config))
        policy = directory / 'policy/probe'
        (policy / 'main.py').write_text(POLICY)
        (policy / 'policy.yaml').write_text(json.dumps(dict(schema_version=3,
            execution=dict(mode='sync',call_every_actions=1,control_mode='qpos'),params={},
            tui_commands={'info':dict(handler='info',args=[],timeout_s=3)})))
        source = directory / 'packages/source-probe/src'
        (source / 'source_probe').mkdir(parents=True)
        module = source / 'source_probe/__init__.py'
        module.write_text("VALUE = 'first'\n")
        stamp = module.stat()
        target = '/workspace/packages/source-probe/src'
        (policy / 'compose.yaml').write_text(json.dumps({'services': {'policy': {
            'image': WORKER_IMAGE, 'environment': {'PYTHONPATH':target, 'RC_SOURCE_MODULES':json.dumps({'source_probe':target})},
            'volumes':[dict(type='bind',source=str(source),target=target,read_only=True,bind={'create_host_path':False})]}}}))
        # Create an intentionally stale, timestamp-valid host cache. The runtime
        # must ignore it even when source timestamps and file size are unchanged.
        cache_args = ['docker','run','--rm','--user',f'{os.getuid()}:{os.getgid()}',
            '--mount',f'type=bind,source={source},target={target}','-e','PYTHONPYCACHEPREFIX=',
            '-e','PYTHONPATH='+target,'--entrypoint','python',WORKER_IMAGE,'-c','import source_probe; print(source_probe.VALUE)']
        assert subprocess.check_output(cache_args,text=True).strip() == 'first'
        rc_file = directory / 'src/robot_control/controller.py'
        rc_original = rc_file.read_text()
        env = dict(os.environ,RC_BASE_PROJECT=project,RC_UID=str(os.getuid()),RC_GID=str(os.getgid()),
            RC_RPC_VOLUME=context['rpc_volume'],RC_DOCKER_GID=gid,RC_DOCKER_SOCKET=endpoint,RC_MCP_PORT='0')
        compose = ['docker','compose','--env-file','/dev/null','--project-directory',str(directory),'-p',project,
            '-f',str(ROOT/'compose.yaml'),'-f',str(directory/'override.json')]
        reports = []
        try:
            for phase in ('first','second'):
                rc_file.write_text(rc_original + f'\nRobotController.source_probe = {phase!r}\n')
                override = {'services': {'ipc-anchor':{'image':ANCHOR_IMAGE}, 'robot-control':{
                    'image':RC_IMAGE,'environment':{'SOURCE_TEST_PHASE':phase},
                    'command':['python3','/validation/source_mount_docker_smoke.py','--container'],
                    'volumes':[dict(type='bind',source=str(ROOT/'tests'),target='/validation',read_only=True,bind={'create_host_path':False})]}}}
                (directory/'override.json').write_text(json.dumps(override))
                subprocess.run(compose+['up','-d','--no-build'],env=env,check=True)
                cid = subprocess.check_output(compose+['ps','-aq','robot-control'],env=env,text=True).strip()
                if phase == 'first':
                    wait(lambda:(directory/'outputs/ready').exists())
                    updated = module.with_suffix('.new')
                    updated.write_text("VALUE = 'other'\n")
                    updated.replace(module)
                    os.utime(module, ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
                    assert subprocess.check_output(cache_args,text=True).strip() == 'first', 'stale cache fixture ineffective'
                    (directory/'outputs/updated').touch()
                    wait(lambda:(directory/'outputs/ready-missing').exists())
                    module.rename(module.with_suffix('.missing'))
                    (directory/'outputs/missing').touch()
                code = subprocess.check_output(['docker','wait',cid],text=True,timeout=160).strip()
                logs = subprocess.check_output(['docker','logs',cid],text=True,stderr=subprocess.STDOUT)
                (evidence/f'{phase}.log').write_text(logs)
                assert code == '0', logs[-10000:]
                reports.append(read_config(directory/f'outputs/{phase}.json'))
                subprocess.run(compose+['down','-v'],env=env,check=True)
                if phase == 'first': module.with_suffix('.missing').rename(module)
            for image, identity in ids.items():
                assert subprocess.check_output(['docker','image','inspect','-f','{{.Id}}',image],text=True).strip() == identity
            report = dict(image_ids=ids,audit=audit,reloads=reports,stale_host_cache_ignored=True)
            (evidence/'result.json').write_text(json.dumps(report,indent=2))
            print(json.dumps(report,indent=2))
        finally:
            owned = subprocess.check_output(['docker','ps','-aq','--filter','label=io.robot-control.deployment='+token],text=True).split()
            if owned: subprocess.run(['docker','rm','-f',*owned],check=True)
            subprocess.run(compose+['down','-v','--remove-orphans'],env=env,check=True)


if __name__ == '__main__':
    in_container() if sys.argv[1:] == ['--container'] else on_host()
