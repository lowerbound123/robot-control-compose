"""Assertions used only by the opt-in, isolated LIFT2 Docker acceptance."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def wait(predicate, timeout=30):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result: return result
        time.sleep(.01)
    raise AssertionError('LIFT2 acceptance condition timed out')


def simulator_events():
    return [json.loads(line) for line in Path('/evidence/simulator-events.jsonl').read_text().splitlines()]


def transport():
    import rospy
    from arm_control.msg import JointControl, JointInformation, PosCmd
    from rc_connector_sdk.client import ConnectorClient
    from rc_connector_sdk.images import map_images
    assert sys.version_info[:2] == (3, 10)
    assert not Path('/opt/conda').exists()
    assert importlib.util.find_spec('cv_bridge') is None
    assert importlib.util.find_spec('torch') is None
    assert JointControl._md5sum == JointInformation._md5sum == '86ad2a9a176a812901cac6f69b936f2e'
    assert PosCmd._md5sum == 'df5f2abbfa9e683f82c8a2751cdc09f0'
    report = {'python': sys.version.split()[0], 'checks': []}
    for mode in ('raw', 'compressed'):
        with tempfile.TemporaryDirectory(prefix='lift2-') as temp:
            endpoint = temp+'/io'
            topics = {name: '/camera_'+suffix+'/color/image_raw'+('/compressed' if mode == 'compressed' else '')
                      for name, suffix in (('head', 'h'), ('left_wrist', 'l'), ('right_wrist', 'r'))}
            config = {'ros_master_uri': 'http://127.0.0.1:11311', 'ros_ip': '127.0.0.1',
                      'camera_transport': mode, 'camera_topics': topics,
                      'connect_timeout_s': 5, 'feedback_timeout_s': .3, 'camera_timeout_s': .3}
            log = open('/evidence/connector-'+mode+'.log', 'w')
            process = subprocess.Popen([sys.executable, '-m', 'rc_connector_sdk', '--source',
                '/workspace/connector/lift2/main.py', '--socket', endpoint, '--config-json', json.dumps(config)],
                stdout=log, stderr=subprocess.STDOUT)
            client = None
            try:
                def ready():
                    assert process.poll() is None, 'connector exited; see connector log'
                    return Path(endpoint+'.ready').exists()
                wait(ready, 10)
                client = ConnectorClient(endpoint)
                assert client.describe()['image_bytes'] == 0
                state = client.get_state()['state']
                assert len(state['qpos']) == len(state['qvel']) == len(state['effort']) == len(state['eef']) == 14
                assert state['eef'][6] == state['qpos'][6]
                assert state['raw']['left']['eef']['gripper'] != state['eef'][6]

                def snapshot():
                    response = client.get_images(list(topics))
                    if len(response['images']) != 3:
                        if response['lease_id']: client.release_images([response['lease_id']])
                        return None
                    return response
                response = wait(snapshot)
                with map_images(response['images']) as frames:
                    for index, name in enumerate(topics):
                        frame = frames[name]
                        assert frame.shape == (2, 3, 3) and not frame.flags.writeable
                        assert frame[0, 0].tolist() == [10+index, 20+index, 30+index]
                    del frame
                    assert client.describe()['leases'] == 1
                client.release_images([response['lease_id']])
                client.release_images([response['lease_id']])
                assert client.describe()['leases'] == 0

                def receipts(count):
                    rows = []
                    wait(lambda: rows.extend(client.poll_results()) or len(rows) == count)
                    return rows
                before = len(simulator_events())
                ids = [client.submit({side: {'mode': 'qpos', 'values': [.1+i*.01]*6+[4.]}
                                      for side in ('left', 'right')}) for i in range(8)]
                rows = receipts(8)
                assert [row['id'] for row in rows] == ids and all(row['success'] for row in rows), rows
                wait(lambda: len(simulator_events()) >= before+16)
                sent = simulator_events()[before:]
                for side in ('left', 'right'):
                    assert [round(e['values'][0], 2) for e in sent if e['arm'] == side] == [.1, .11, .12, .13, .14, .15, .16, .17]
                before = len(simulator_events())
                for modes in ({'left': 'epos'}, {'right': 'epos'}, {'left': 'qpos', 'right': 'epos'},
                              {'right': 'qpos', 'left': 'epos'}):
                    client.submit({side: {'mode': value, 'values': [9.]*7} for side, value in modes.items()})
                    row, = receipts(1)
                    assert not row['success'] and 'epos' in row['error'] and row['events'] == []
                time.sleep(.1)
                assert len(simulator_events()) == before
                history = client.get_state({'left': 0, 'right': 0})
                assert history['history']['left'] and history['history']['right']
                rospy.set_param('/lift2_test/pause_eef', True)
                wait(lambda: 'stale' in (client.get_state()['state_error'] or ''))
                rospy.set_param('/lift2_test/pause_eef', False)
                wait(lambda: client.get_state()['state_error'] is None)
                rospy.set_param('/lift2_test/pause_images', True)
                time.sleep(.4)
                response = client.get_images(list(topics))
                assert len(response['images']) == 3
                assert all(not frame['valid'] and 'stale' in frame['error'] for frame in response['metadata'].values())
                client.release_images([response['lease_id']])
                rospy.set_param('/lift2_test/pause_images', False)
                client.close(timeout=5)
                assert process.wait(timeout=5) == 0
                report['checks'].append(mode+': MD5, state, pixels, readonly leases, FIFO, epos rejection, stale feedback/images, clean exit')
            finally:
                rospy.set_param('/lift2_test/pause_eef', False)
                rospy.set_param('/lift2_test/pause_images', False)
                if client: client.disconnect()
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()
                log.close()
    Path('/evidence/transport.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


def controller():
    from robot_control.controller import RobotController
    root = Path('/workspace')
    assert importlib.util.find_spec('rospy') is None and importlib.util.find_spec('rclpy') is None
    rc = RobotController(root/'configs/rc.mock.yaml')
    report = {'checks': [], 'rc_python': sys.version.split()[0]}

    def command(line):
        identity = rc.submit(line)
        reply = rc.wait_operation(identity, 40)
        assert reply['status'] == 'succeeded', reply
        return reply.get('result')

    def members():
        if hasattr(rc._worker, 'member_workers'): return rc._worker.member_workers()
        return {rc._worker.config['id']: rc._worker}

    def identities():
        return {name: {'container': w.endpoint.container_id, 'pid': w.pid,
                        'runtime': w.runtime_id, 'host': w.host.instance_id}
                for name, w in members().items()}

    def exercise(label):
        baseline = rc.status()['consumed']
        command('resume')
        wait(lambda: rc.status()['consumed'] >= baseline+4)
        command('pause')
        names = list(members())
        for name in names:
            values = command('policy '+name+' example')
            images = json.loads(values['images'])
            assert len(images) == 3 and all(v['readonly'] for v in images.values()), images
            assert all(v['shape'] == [2, 3, 3] for v in images.values()), images
            assert values['python'].startswith('3.14.')
        frames = rc.robot.camera_preview_snapshot()
        assert set(frames) == {'head', 'left_wrist', 'right_wrist'}
        assert frames['head']['image'][0, 0].tolist() == [10, 20, 30]
        command('record start')
        wait(lambda: rc.status()['recording']['samples_written'] >= 3)
        command('record stop')
        old, workers = rc.robot.raw, identities()
        command('connector switch mock')
        assert identities() == workers and old.closed
        assert rc.status()['state'] == 'paused'
        command('connector switch lift2')
        assert identities() == workers and rc.status()['state'] == 'paused'
        assert rc.robot.raw.client.info['runtime_id'] != old.client.info['runtime_id']
        report['checks'].append(label+': qpos, readonly RGB, preview, recording, mock/lift2 switch keeps policy containers/PIDs')
        report[label] = workers

    rc.start()
    try:
        wait(lambda: rc.status()['policy'] == 'example' and rc.status()['state'] == 'paused', 45)
        exercise('example')
        command('policy switch example_chain')
        assert set(identities()) == {'example-0', 'example-1'}
        exercise('self_chain')
        command('policy switch example')
        command('policy example config set execution.control_mode epos')
        consumed_before = rc.status()['consumed']
        command('resume')
        wait(lambda: rc.status()['state'] == 'fault')
        assert rc.status()['consumed'] == consumed_before
        assert any('epos' in event['message'] for event in rc.read_events()['events'])
        count = rc.robot.raw.client.serial
        time.sleep(.15)
        assert rc.robot.raw.client.serial == count
        assert rc.robot.read_state()['qpos']
        report['checks'].append('epos uses unchanged RC failure path: fault, no consumption, no new actions, queries available')
    finally:
        rc.submit('exit')
        assert rc.done.wait(35), rc.status()
        assert rc.exit_code == 0 and rc.robot.closed and rc._pool._bytes == 0, rc.read_events()
    report['clean_exit'] = True
    (root/'outputs/lift2-acceptance.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('transport', 'controller'))
    args = parser.parse_args()
    globals()[args.mode]()
