"""Isolated real RC/SDK/policy containers talking to a local MaaS peer.

RC_TEST_RC_IMAGE must include RC's test dependencies plus websockets,
msgpack for the independent __ndarray__ test peer. No real service or hardware.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from config_helpers import read_config
from internvla_maas_helpers import ROOT, images, model_server, settings, wait


def container():
    from robot_control.controller import RobotController
    from robot_control.robots import MockRobot
    from source_helpers import source_report

    class CameraRobot(MockRobot):
        def read_images(self):
            return {name: (frame, time.monotonic()) for name, frame in images().items()}

    root = Path('/workspace')
    report = {'rc_python': list(sys.version_info[:2]), 'checks': [], 'sources': source_report()}
    with model_server(host='0.0.0.0') as server:
        directory = root / 'policy/internvla_maas'
        raw = settings(f'ws://robot-control:{server.port}')
        (directory / 'model.json').write_text(json.dumps(raw))
        raw['runtime']['instruction'] = 'Docker second task.'
        raw['actions'] = {'gripper_compensation': False}
        (directory / 'alternate.json').write_text(json.dumps(raw))
        controller = RobotController(root / 'configs/rc.mock.yaml', robot=CameraRobot())

        def command(line):
            identity = controller.submit(line)
            receipt = controller.wait_operation(identity, 45)
            assert receipt['status'] == 'succeeded', receipt

        controller.start()
        try:
            wait(lambda: controller._operation is None and controller._worker is not None, 40)
            assert controller.status()['state'] == 'paused', controller.read_events()
            old = controller._worker
            version = subprocess.check_output(['docker', 'exec', old.endpoint.container_id,
                                               'python', '--version'], text=True).strip()
            assert version.startswith('Python 3.11.'), version
            report['policy_python'] = version
            initial_id = old.endpoint.container_id
            assert len(server.hellos) == 1 and not server.requests
            command('resume')
            wait(lambda: controller.status()['consumed'] >= 24, 10)
            command('pause')
            obs = server.requests[0]
            assert set(obs['images']) == {'front', 'wrist_left', 'wrist_right'}
            assert obs['images']['front'][112, 112, 0] == 1
            assert all(frame.shape == (224, 224, 3) for frame in obs['images'].values())
            assert obs['state']['joint_pos'].shape == (14,)
            report['checks'].append('Python3.10 RC to Python3.11 policy; binary MaaS handshake, 224x224 letterboxed RGB, state and action consumption')

            command('policy internvla_maas switch-model alternate.json')
            current = controller._worker
            assert current.endpoint.container_id != initial_id
            assert old.cleanup_confirmed and not old.has_readers
            assert controller.status()['state'] == 'paused'
            report['reload'] = {'before': initial_id, 'after': current.endpoint.container_id}
            report['checks'].append('cold switch replaces only target after reader/container exit proof and stays paused')

            server.responses.put({'body': {'type': 'stop', 'reason': 'fixture-key remote stop'}})
            command('resume')
            wait(lambda: controller.status()['state'] == 'fault' and controller._worker is None, 15)
            consumed, requests, hellos = controller.status()['consumed'], len(server.requests), len(server.hellos)
            assert controller.status()['queue_remaining'] == 0
            time.sleep(.2)
            assert controller.status()['consumed'] == consumed
            assert len(server.requests) == requests and len(server.hellos) == hellos
            assert server.requests[-1]['instruction'] == 'Docker second task.'
            assert 'fixture-key' not in json.dumps(controller.status())
            assert 'fixture-key' not in json.dumps(controller.read_events())
            report['checks'].append('stop clears executable queue, redacts credentials, and does not reconnect/replay')

            command('policy switch internvla_maas')
            assert controller.status()['state'] == 'paused'
            command('resume')
            wait(lambda: controller.status()['consumed'] > consumed, 10)
            report['checks'].append('manual cold reload creates a new session and resumes only on explicit resume')
        except BaseException:
            print(json.dumps(controller.read_events(), ensure_ascii=False), flush=True)
            raise
        finally:
            controller.submit('exit')
            assert controller.done.wait(30), controller.status()
        assert controller.exit_code == 0 and controller._pool._bytes == 0
        report.update(exit_code=controller.exit_code, requests=len(server.requests), sessions=len(server.hellos))
        (root / 'outputs/maas-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)


def host(evidence):
    from project_templates import copy_templates
    from source_helpers import anchor_override
    sys.path.insert(0, str(ROOT / 'scripts'))
    from setup_project import setup

    evidence.mkdir(parents=True, exist_ok=True)
    rc_image = os.environ.get('RC_TEST_RC_IMAGE', 'rc-internvla-maas-rc-tests:20261104')
    policy_image = os.environ.get('RC_TEST_MAAS_IMAGE', 'robot-control-compose-internvla-maas:local')
    with tempfile.TemporaryDirectory(prefix='rc-maas-smoke-') as temporary:
        directory = Path(temporary)
        for name in ('src', 'packages/policy-sdk', 'packages/internvla-maas-policy', 'docker'):
            shutil.copytree(ROOT / name, directory / name, ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))
        (directory / 'configs').mkdir()
        shutil.copyfile(ROOT / '.env.example', directory / '.env.example')
        source = copy_templates(ROOT / 'policy/internvla_maas', directory / 'policy/internvla_maas')
        rc = read_config(ROOT / 'configs/rc.mock.yaml.example')
        rc.update(startup_policy='internvla_maas', frequency_hz=30,
                  timeouts=dict(initialize_s=35, inference_s=65, release_s=8, shutdown_s=25))
        rc['safety']['enabled'] = False
        (directory / 'configs/rc.mock.yaml').write_text(json.dumps(rc))
        setup(directory, ['internvla_maas'], config_only=True)
        state_path = directory / '.setup/runtime.json'
        context = read_config(state_path)
        context['docker_gid'] = int(subprocess.check_output([
            'docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'stat',
            '--mount', f'type=bind,source={context["docker_socket"]},target=/socket',
            policy_image, '-c', '%g', '/socket'], text=True, timeout=30).strip())
        state_path.write_text(json.dumps(context))
        model = json.loads(subprocess.check_output(['docker', 'compose', '--env-file', '/dev/null',
            '-f', str(source / 'compose.yaml'), 'config', '--format', 'json'],
            env=dict(os.environ, RC_HOST_PROJECT_ROOT=temporary)))
        model['services']['policy'].update(image=policy_image, networks=['model'])
        model['networks'] = {'model': {'external': True, 'name': context['base_project'] + '_default'}}
        (source / 'compose.yaml').write_text(json.dumps(model))
        override = {'services': {'robot-control': {
            'image': rc_image, 'command': ['python3', '/validation/internvla_maas_docker_smoke.py', '--container'],
            'volumes': [{'type': 'bind', 'source': str(ROOT / 'tests'), 'target': '/validation', 'read_only': True}]}}}
        anchor_override(override['services'])
        (directory / 'override.json').write_text(json.dumps(override))
        env = dict(os.environ, RC_BASE_PROJECT=context['base_project'], RC_UID=str(context['uid']),
                   RC_GID=str(context['gid']), RC_DOCKER_GID=str(context['docker_gid']),
                   RC_DOCKER_SOCKET=context['docker_socket'], RC_RPC_VOLUME=context['rpc_volume'], RC_MCP_PORT='0')
        compose = ['docker', 'compose', '--env-file', '/dev/null', '--project-directory', temporary,
                   '-p', context['base_project'], '-f', str(ROOT / 'compose.yaml'), '-f', str(directory / 'override.json')]
        try:
            subprocess.run(compose + ['up', '-d', '--no-build'], env=env, check=True, timeout=90)
            cid = subprocess.check_output(compose + ['ps', '-aq', 'robot-control'], env=env, text=True).strip()
            try:
                code = subprocess.check_output(['docker', 'wait', cid], text=True, timeout=180).strip()
            finally:
                logs = subprocess.check_output(['docker', 'logs', cid], text=True, stderr=subprocess.STDOUT)
                (evidence / 'container.log').write_text(logs)
            output = directory / 'outputs/maas-smoke.json'
            if output.exists():
                shutil.copyfile(output, evidence / 'result.json')
            assert code == '0', logs[-6000:]
            print(output.read_text(), flush=True)
        finally:
            ids = subprocess.check_output(['docker', 'ps', '-aq', '--filter',
                'label=io.robot-control.deployment=' + context['deployment_id']], text=True).split()
            if ids:
                subprocess.run(['docker', 'rm', '-f', *ids], check=True, timeout=60)
            subprocess.run(compose + ['down', '-v', '--remove-orphans'], env=env, check=True, timeout=90)
            remaining = {}
            for resource in ('container', 'volume', 'network'):
                remaining[resource] = subprocess.check_output(['docker', resource, 'ls', '-q', '--filter',
                    'label=com.docker.compose.project=' + context['base_project']], text=True).split()
            (evidence / 'cleanup.json').write_text(json.dumps({'project': context['base_project'], **remaining}, indent=2))
            assert not any(remaining.values()), remaining


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', action='store_true')
    parser.add_argument('--evidence-dir', type=Path, default=Path('/tmp/rc-internvla-maas-evidence'))
    args = parser.parse_args()
    container() if args.container else host(args.evidence_dir)
