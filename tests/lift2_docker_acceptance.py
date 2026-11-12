"""Opt-in LIFT2-only Docker acceptance; simulator has no external network.

Requires built RC, anchor, example, mock, lift2 runtime and lift2 simulator images.
Run: python3 tests/lift2_docker_acceptance.py --evidence-dir /tmp/lift2-evidence
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
import uuid

from config_helpers import read_config
from project_templates import copy_templates
from source_helpers import copy_connectors


ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def capture(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def main(evidence):
    sys.path.insert(0, str(ROOT/'scripts'))
    from setup_project import setup
    evidence = evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:10]
    sim = 'rc-lift2-sim-'+tag
    transport_name = 'rc-lift2-probe-'+tag
    runtime_image = os.environ.get('RC_TEST_LIFT2_IMAGE', 'robot-control-compose-lift2-connector:local')
    simulator_image = os.environ.get('RC_TEST_LIFT2_SIM_IMAGE', 'robot-control-compose-lift2-simulator:test')
    for image in (runtime_image, simulator_image, 'robot-control-compose-mock-connector:local',
                  'robot-control-compose-example:local',
                  os.environ.get('RC_TEST_RC_IMAGE', 'robot-control-compose-rc:local'),
                  os.environ.get('RC_TEST_ANCHOR_IMAGE', 'robot-control-compose-anchor:local')):
        capture(['docker', 'image', 'inspect', '--format', '{{.Id}}', image])
    uid = f'{os.getuid()}:{os.getgid()}'
    directory = Path(tempfile.mkdtemp(prefix='rc-lift2-deployment-'))
    context, compose, env = None, None, None
    simulator_started = False
    try:
        (evidence/'simulator-events.jsonl').write_text('')
        (evidence/'simulator-ready').unlink(missing_ok=True)
        run(['docker', 'run', '-d', '--name', sim, '--network', 'none', '--read-only',
             '--tmpfs', '/tmp:rw,nosuid,size=128m', '--cap-drop', 'ALL', '--user', uid,
             '-e', 'HOME=/tmp', '-e', 'ROS_MASTER_URI=http://127.0.0.1:11311', '-e', 'ROS_IP=127.0.0.1',
             '-e', 'PYTHONDONTWRITEBYTECODE=1',
             '--mount', f'type=bind,source={ROOT / "tests"},target=/validation,readonly',
             '--mount', f'type=bind,source={evidence},target=/evidence', simulator_image,
             '/bin/bash', '-c', '. /opt/lift2_msgs/devel/setup.bash; exec python3 /validation/lift2_simulator.py'],
            stdout=subprocess.DEVNULL)
        simulator_started = True
        deadline = time.monotonic()+30
        while not (evidence/'simulator-ready').exists():
            assert time.monotonic() < deadline, 'simulator did not become ready'
            assert capture(['docker', 'inspect', '-f', '{{.State.Running}}', sim]) == 'true'
            time.sleep(.1)
        with (evidence/'transport-run.log').open('w') as log:
            run(['docker', 'run', '--rm', '--name', transport_name, '--network', 'container:'+sim,
                 '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=128m', '--cap-drop', 'ALL', '--user', uid,
                 '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=/workspace/packages/connector-sdk/src',
                 '-e', 'ROS_MASTER_URI=http://127.0.0.1:11311', '-e', 'ROS_IP=127.0.0.1',
                 '--mount', f'type=bind,source={ROOT / "tests"},target=/validation,readonly',
                 '--mount', f'type=bind,source={ROOT / "connector/lift2"},target=/workspace/connector/lift2,readonly',
                 '--mount', f'type=bind,source={ROOT / "packages/connector-sdk"},target=/workspace/packages/connector-sdk,readonly',
                 '--mount', f'type=bind,source={evidence},target=/evidence', runtime_image,
                 'python3', '/validation/lift2_acceptance_runtime.py', 'transport'],
                stdout=log, stderr=subprocess.STDOUT, timeout=90)

        for name in ('src', 'packages/policy-sdk', 'packages/connector-sdk', 'docker'):
            shutil.copytree(ROOT/name, directory/name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.egg-info', '._*'))
        copy_connectors(directory)
        for name in ('example', 'example_chain'):
            copy_templates(ROOT/'policy'/name, directory/'policy'/name)
        shutil.copyfile(ROOT/'.env.example', directory/'.env.example')
        (directory/'configs/robots').mkdir(parents=True)
        device = read_config(ROOT/'connector/lift2/devices/robot-main.yaml.example')
        device.update(ros_master_uri='http://127.0.0.1:11311', ros_ip='127.0.0.1')
        (directory/'connector/lift2/devices/robot-main.yaml').write_text(json.dumps(device))
        # Only this isolated deployment redirects the connector into the
        # simulator's network namespace. Production remains host networking.
        (directory/'connector/lift2/compose.yaml').write_text(json.dumps({'services': {'connector': {
            'image': runtime_image, 'network_mode': 'container:'+sim, 'restart': 'no'}}}))
        for name, config_file in (('lift2', 'devices/robot-main.yaml'), ('mock', 'devices/default.yaml')):
            (directory/f'configs/robots/{name}.yaml').write_text(json.dumps({
                'connector': 'body', 'source': name, 'config': config_file}))
        config = read_config(ROOT/'configs/rc.mock.yaml.example')
        config.update(startup_policy='example', frequency_hz=30,
                      connector_directory='../connector', robot_profiles_directory='robots', robot={'profile': 'lift2'})
        config['timeouts'] = dict(initialize_s=30, inference_s=5, release_s=3, shutdown_s=25)
        (directory/'configs/rc.mock.yaml').write_text(json.dumps(config))
        setup(directory, ['example_chain'], config_only=True)
        context = read_config(directory/'.setup/runtime.json')
        override = {'services': {'robot-control': {
            'image': os.environ.get('RC_TEST_RC_IMAGE', 'robot-control-compose-rc:local'),
            'command': ['python3', '/validation/lift2_acceptance_runtime.py', 'controller'],
            'volumes': [{'type': 'bind', 'source': str(ROOT/'tests'), 'target': '/validation', 'read_only': True}],
        }, 'ipc-anchor': {'image': os.environ.get('RC_TEST_ANCHOR_IMAGE', 'robot-control-compose-anchor:local')}}}
        (directory/'override.json').write_text(json.dumps(override))
        env = dict(os.environ, RC_BASE_PROJECT=context['base_project'], RC_UID=str(context['uid']),
                   RC_GID=str(context['gid']), RC_DOCKER_GID=str(context['docker_gid']),
                   RC_DOCKER_SOCKET=context['docker_socket'], RC_RPC_VOLUME=context['rpc_volume'],
                   RC_CONNECTOR_RPC_VOLUME=context['connector_rpc_volume'], RC_MCP_PORT='0')
        compose = ['docker', 'compose', '--env-file', '/dev/null', '--project-directory', str(directory),
                   '-p', context['base_project'], '-f', str(ROOT/'compose.yaml'), '-f', str(directory/'override.json')]
        run(compose+['up', '-d', '--no-build'], env=env)
        cid = capture(compose+['ps', '-aq', 'robot-control'], env=env)
        code = capture(['docker', 'wait', cid], timeout=180)
        assert code == '0', 'RC acceptance failed; inspect controller.log'
        shutil.copyfile(directory/'outputs/lift2-acceptance.json', evidence/'controller.json')
        print(json.dumps({'transport': read_config(evidence/'transport.json'),
                          'controller': read_config(evidence/'controller.json')}, indent=2))
    finally:
        # Every cleanup target is a unique name or this deployment's exact label.
        if context:
            owned = capture(['docker', 'ps', '-aq', '--filter', 'label=io.robot-control.deployment='+context['deployment_id']]).split()
            for cid in owned:
                logs = subprocess.run(['docker', 'logs', cid], text=True, capture_output=True)
                (evidence/('owned-'+cid[:12]+'.log')).write_text(logs.stdout+logs.stderr)
            if owned: run(['docker', 'rm', '-f', *owned], stdout=subprocess.DEVNULL)
        if compose:
            cid = capture(compose+['ps', '-aq', 'robot-control'], env=env)
            if cid:
                logs = subprocess.run(['docker', 'logs', cid], text=True, capture_output=True)
                (evidence/'controller.log').write_text(logs.stdout+logs.stderr)
            run(compose+['down', '-v', '--remove-orphans'], env=env)
        if simulator_started:
            logs = subprocess.run(['docker', 'logs', sim], text=True, capture_output=True)
            (evidence/'simulator.log').write_text(logs.stdout+logs.stderr)
            if capture(['docker', 'ps', '-aq', '--filter', 'name=^/'+transport_name+'$']):
                run(['docker', 'rm', '-f', transport_name], stdout=subprocess.DEVNULL)
            run(['docker', 'stop', '--time', '10', sim], stdout=subprocess.DEVNULL)
            run(['docker', 'rm', sim], stdout=subprocess.DEVNULL)
        remaining = []
        if context:
            remaining = capture(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project='+context['base_project']]).split()
            assert not remaining
            assert not capture(['docker', 'volume', 'ls', '-q', '--filter', 'label=com.docker.compose.project='+context['base_project']])
        (evidence/'cleanup.json').write_text(json.dumps({'project': context and context['base_project'],
            'simulator': sim, 'remaining_containers': remaining, 'deployment_directory': str(directory)}))
        shutil.rmtree(directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', required=True, type=Path)
    main(parser.parse_args().evidence_dir)
