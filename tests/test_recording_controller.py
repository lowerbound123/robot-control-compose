import csv
import json
import time
from pathlib import Path

import pytest

from test_controller import setup_controller, ready, until


def operation(controller, command):
    identity = controller.submit(command)
    until(lambda: controller.get_operation(identity)['status'] in ('succeeded','failed','cancelled'), 8)
    result = controller.get_operation(identity)
    assert result['status']=='succeeded', result
    return result['result']


@pytest.mark.parametrize('hz', [1, 30, 100])
def test_recording_is_independent_of_control_and_pause_and_mcp(setup_controller,hz):
    controller, robot, _ = setup_controller
    controller.config['frequency_hz']=hz
    worker=ready(controller)
    generation=controller.buffer.generation
    status=operation(controller,'record on')
    assert status['state']=='recording'
    assert controller._worker is worker and controller.buffer.generation==generation
    until(lambda:controller.status()['recording']['samples_written']>=8,5)
    operation(controller,'take')
    until(lambda:controller.status()['recording']['samples_written']>=12,5)
    status=operation(controller,'record off')
    assert controller.control_owner=='mcp'
    with (Path(status['path'])/'qpos.csv').open() as f: rows=list(csv.reader(f))
    assert len(rows)>=13
    with pytest.raises(PermissionError):controller.submit('record on',origin='mcp')
    assert controller.query('status')['recording']['state']=='idle'


def test_inference_images_and_config_and_send_events_are_saved(setup_controller):
    controller,robot,_=setup_controller
    worker=ready(controller)
    operation(controller,'record on')
    operation(controller,'resume')
    until(lambda:worker.requests)
    worker.finish([[.1]*14])
    until(lambda:controller.buffer.consumed>0)
    status=operation(controller,'record off')
    folder=Path(status['path'])
    policy=[json.loads(x) for x in (folder/'policy.jsonl').read_text().splitlines()]
    observation=next(x for x in policy if x['event']=='policy.observation')
    assert set(observation['image_files'])=={'head','left_wrist','right_wrist'}
    assert all((folder/f['file']).is_file() for f in observation['image_files'].values())
    controls=[json.loads(x) for x in (folder/'control.jsonl').read_text().splitlines()]
    assert any(x['event']=='control.published' for x in controls)
    assert any(x['event']=='control.attempt' for x in controls)
    assert (folder/'configs/initial.json').is_file()


def test_recording_keeps_session_across_switch_and_preserves_config_history(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    first = operation(controller, 'record on')
    operation(controller, 'set policy.params.value 7')
    operation(controller, 'switch second')
    assert controller.status()['recording']['session_id'] == first['session_id']
    operation(controller, 'set global.record_marker 42')
    until(lambda:controller._record_global_version == controller._global_service.version)
    status = operation(controller, 'record off')
    configs = [json.loads(path.read_text()) for path in (Path(status['path'])/'configs').glob('*.json')]
    snapshots = [data for data in configs if 'instances' in data]
    assert any(data['instances'][0]['config']['params']['value'] == 1 for data in snapshots)
    assert any(data['instances'][0]['config']['params']['value'] == 7 for data in snapshots)
    assert any(data['instances'][0]['instance_id'] == 'second' for data in snapshots)
    assert any(data.get('kind') == 'globals' and data['value'].get('record_marker') == 42 for data in configs)


def test_recording_mcp_mixed_modes_and_sync_inference_wait(setup_controller):
    from robot_control.direct_commands import structured_step
    controller, _, _ = setup_controller
    worker = ready(controller)
    operation(controller, 'record on')
    operation(controller, 'resume')
    until(lambda: len(worker.requests) == 1)
    before = controller.status()['recording']['samples_written']
    until(lambda:controller.status()['recording']['samples_written'] >= before + 4)
    operation(controller, 'take')
    identity = controller.submit_direct([structured_step(4,
        {'left': {'end_pos':[.01,0,0,0,0,0], 'gripper':-3.4}}, 'fast')])
    assert controller.wait_operation(identity, 3)['status'] == 'succeeded'
    after = controller.status()['recording']['samples_written']
    until(lambda:controller.status()['recording']['samples_written'] >= after + 3)
    status = operation(controller, 'record off')
    assert controller.control_owner == 'mcp' and controller.buffer.consumed == 0
    rows = {}
    for mode in ('epos','qpos'):
        with (Path(status['path'])/f'control_{mode}.csv').open() as stream:
            rows[mode] = list(csv.reader(stream))[1:]
    assert any(row[3:10] == ['']*7 and row[10:] == ['']*7 for row in rows['epos'])
    assert any(any(left[3:10]) and left[10:] == ['']*7
               and right[3:10] == ['']*7 and any(right[10:])
               for left, right in zip(rows['epos'], rows['qpos']))
    # Closing recording never waits for the outstanding policy inference.
    assert controller._inflight is not None


def test_record_help_status_and_invalid_arguments_do_not_change_generation(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    generation = controller.buffer.generation
    assert 'record on/off' in operation(controller, 'help record')
    assert operation(controller, 'record status')['state'] == 'idle'
    for line in ('record invalid', 'record on extra'):
        identity = controller.submit(line)
        assert controller.wait_operation(identity, 2)['status'] == 'failed'
    assert controller.buffer.generation == generation


def test_inference_records_actual_leased_frame_when_image_pool_is_full(setup_controller):
    import numpy as np
    from PIL import Image
    from rc_policy_sdk.images import map_images
    controller, robot, _ = setup_controller
    ready(controller)
    operation(controller, 'record on')
    leases = []
    try:
        with controller._lock:
            for number in (1, 2, 3):
                for camera in ('head', 'left_wrist', 'right_wrist'):
                    assert controller._pool.publish(camera, np.full((2,3,3), number, np.uint8), float(number))
                leases.append(controller._pool.acquire())
            state, lease, runtime = controller._snapshot()
            leases.append(lease)
            with map_images(lease.descriptors) as images:
                expected = images['head'].copy()
            assert int(expected[0,0,0]) == 3
            before = [slot['leases'] for slots in controller._pool._slots.values() for slot in slots]
        status = operation(controller, 'record off')
        policy = [json.loads(row) for row in (Path(status['path'])/'policy.jsonl').read_text().splitlines()]
        observation = next(row for row in policy if row.get('observation_id') == runtime['observation_id'])
        frame = observation['image_files']['head']
        with Image.open(Path(status['path'])/frame['file']) as image:
            np.testing.assert_array_equal(np.asarray(image), expected)
        assert frame['timestamp'] == 3.0
        assert before == [slot['leases'] for slots in controller._pool._slots.values() for slot in slots]
    finally:
        for lease in leases:
            lease.release()
