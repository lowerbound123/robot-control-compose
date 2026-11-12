"""LIFT2 mapping/validation with the external ROS transport replaced only."""
import copy
import importlib.util
import io
from pathlib import Path
import threading
import time
from types import SimpleNamespace as NS

import numpy as np
from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def lift2():
    spec = importlib.util.spec_from_file_location('tested_lift2', ROOT / 'connector/lift2/main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def joint(offset=0):
    return NS(joint_pos=[offset+i for i in range(7)], joint_vel=[.1]*7,
              joint_cur=[.2]*7, mode=0)


def eef():
    return NS(x=.1, y=.2, z=.3, roll=.4, pitch=.5, yaw=.6, gripper=.7,
              quater_x=0., quater_y=0., quater_z=0., quater_w=1.,
              chx=0., chy=0., chz=0., vel_l=0., vel_r=0., height=0.,
              head_pit=0., head_yaw=0., tempFloatData=[0.]*6,
              tempIntData=[0]*6, mode1=0, mode2=0, timeCount=0)


def raw_image(data=bytes([1, 2, 3, 4, 5, 6]), **kw):
    return NS(height=1, width=2, encoding='rgb8', step=6, is_bigendian=0,
              data=data, **kw)


def wait_for(predicate, timeout=2):
    deadline = time.monotonic()+timeout
    while not predicate():
        assert time.monotonic() < deadline, 'condition timed out'
        time.sleep(.005)


class Publisher:
    def __init__(self):
        self.messages = []
        self.connections = 1
        self.error = None
        self.closed = False

    def get_num_connections(self): return self.connections
    def unregister(self): self.closed = True

    def publish(self, message):
        if self.error: raise RuntimeError(self.error)
        self.messages.append(message)


@pytest.fixture
def device(lift2, monkeypatch):
    pubs, subscriptions = {}, []

    def subscribe(topic, message_type, callback, **kwargs):
        subscriptions.append(topic)
        if 'joint_information' in topic: callback(joint(10 if topic.endswith('2') else 0))
        if 'pos_back' in topic: callback(eef())
        return NS(unregister=lambda: None)

    def publisher(topic, message_type, **kwargs):
        assert kwargs.get('queue_size') is None and not kwargs.get('latch', False)
        pubs[topic] = Publisher()
        return pubs[topic]

    ros = NS(init_node=lambda *a, **k: None, is_shutdown=lambda: False,
             Subscriber=subscribe, Publisher=publisher, signal_shutdown=lambda reason: None)
    binding = NS(rospy=ros, JointInformation=object, JointControl=lambda: joint(),
                 PosCmd=object, Image=object, CompressedImage=object)
    monkeypatch.setattr(lift2, '_load_ros', lambda: binding)
    robot = lift2.create_connector({'ros_master_uri': 'http://127.0.0.1:11311',
                                    'ros_ip': '127.0.0.1', 'connect_timeout_s': .1})
    robot.connect()
    yield robot, pubs, ros
    robot.close()


def test_complete_state_uses_native_joint_gripper_and_retains_raw_pose(device):
    robot, _, _ = device
    state = robot.read_state()
    assert state['qpos'] == list(range(7))+list(range(10, 17))
    assert state['qvel'] == [.1]*14 and state['effort'] == [.2]*14
    assert state['eef'] == [.1, .2, .3, .4, .5, .6, 6, .1, .2, .3, .4, .5, .6, 16]
    assert state['raw']['left']['eef']['gripper'] == .7
    assert state['raw']['left']['joint']['mode'] == 0
    assert state['sequences'] == {'left': 1, 'right': 1}
    again = robot.read_state()
    assert again['timestamps'] == state['timestamps']
    assert again['sequences'] == state['sequences']
    state['qpos'][0] = 999
    assert robot.read_state()['qpos'][0] == 0


def test_history_is_new_joint_feedback_only_and_bounded(device):
    robot, _, _ = device
    for i in range(520): robot._on_joint('left', joint(i))
    before = robot.read_state()['sequences']
    robot._on_eef('left', eef())
    assert robot.read_state()['sequences'] == before
    rows = robot.feedback_since('left', 0)
    assert len(rows) == 512 and rows[-1]['seq'] == 521
    assert rows[-1]['qpos'] == list(range(519, 526))
    assert rows[-1]['epos'][-1] == 525
    assert robot.feedback_since('left', 521) == []


@pytest.mark.parametrize('kind', ['joint', 'eef'])
def test_new_invalid_feedback_invalidates_state_and_preserves_recording(device, kind):
    robot, _, _ = device
    message = joint() if kind == 'joint' else eef()
    if kind == 'joint': message.joint_pos[0] = float('nan')
    else: message.x = float('inf')
    getattr(robot, '_on_'+kind)('left', message)
    with pytest.raises(RuntimeError, match='left'): robot.read_state()
    snap = robot.recording_snapshot()
    assert not snap['state_valid'] and snap['state'] is not None
    assert snap['feedback_errors']


def test_eef_freshness_is_required_even_with_new_joint_feedback(device):
    robot, _, _ = device
    robot._feedback_timeout = .01
    time.sleep(.02)
    robot._on_joint('left', joint())
    robot._on_joint('right', joint(10))
    with pytest.raises(RuntimeError, match='stale.*eef'): robot.read_state()


def target(mode='qpos', value=1): return {'mode': mode, 'values': [value]*7}


@pytest.mark.parametrize('targets', [
    {'left': target('epos')}, {'right': target('epos')},
    {'left': target(), 'right': target('epos')},
    {'right': target(), 'left': target('epos')},
])
def test_epos_rejects_entire_request_without_any_publication(device, targets):
    robot, pubs, _ = device
    events = []
    robot.set_command_observer(events.append)
    with pytest.raises(ValueError, match='epos'): robot.execute_arms(targets)
    assert all(not p.messages for p in pubs.values()) and events == []


def test_independent_arm_messages_and_publication_observer(device):
    robot, pubs, _ = device
    events = []
    robot.set_command_observer(events.append)
    robot.execute_arms({'left': target(value=1), 'right': target(value=2)})
    left, right = pubs['/joint_control'].messages[0], pubs['/joint_control2'].messages[0]
    assert left is not right
    assert left.joint_pos == [1]*7 and right.joint_pos == [2]*7
    assert left.joint_vel == [0]*7 and left.joint_cur == [0]*7 and left.mode == 0
    assert [e['arm'] for e in events] == ['left', 'right']
    assert all(e['mode'] == 'qpos' for e in events)


def test_missing_subscriber_is_checked_before_either_arm_publishes(device):
    robot, pubs, _ = device
    pubs['/joint_control2'].connections = 0
    with pytest.raises(RuntimeError, match='right.*subscriber'):
        robot.execute_arms({'left': target(), 'right': target()})
    assert all(not p.messages for p in pubs.values())


def test_partial_publish_keeps_only_successful_arm_event_and_does_not_replay(device):
    robot, pubs, _ = device
    events = []
    robot.set_command_observer(events.append)
    pubs['/joint_control2'].error = 'transport failed'
    with pytest.raises(RuntimeError, match='right'):
        robot.execute_arms({'left': target(), 'right': target()})
    assert [e['arm'] for e in events] == ['left']
    assert len(pubs['/joint_control'].messages) == 1


def test_unrepresentable_float32_target_is_rejected_before_either_publish(device):
    robot, pubs, _ = device
    with pytest.raises(ValueError, match='float32'):
        robot.execute_arms({'left': target(), 'right': target(value=1e100)})
    assert all(not p.messages for p in pubs.values())


@pytest.mark.parametrize('encoding,payload,want', [
    ('rgb8', [1, 2, 3, 4, 5, 6], [[[1, 2, 3], [4, 5, 6]]]),
    ('bgr8', [3, 2, 1, 6, 5, 4], [[[1, 2, 3], [4, 5, 6]]]),
    ('rgba8', [1, 2, 3, 7, 4, 5, 6, 8], [[[1, 2, 3], [4, 5, 6]]]),
    ('bgra8', [3, 2, 1, 7, 6, 5, 4, 8], [[[1, 2, 3], [4, 5, 6]]]),
    ('mono8', [7, 9], [[[7, 7, 7], [9, 9, 9]]]),
])
def test_raw_camera_encodings_and_readonly_rgb(lift2, encoding, payload, want):
    message = raw_image(bytes(payload))
    message.encoding, message.step = encoding, len(payload)
    decoded = lift2._decode_image(message, 'raw')
    assert decoded.tolist() == want and decoded.dtype == np.uint8
    assert not decoded.flags.writeable and decoded.flags.c_contiguous


def test_raw_camera_row_padding_does_not_become_pixels(lift2):
    message = raw_image(bytes([1, 2, 3, 99, 4, 5, 6, 98]))
    message.height, message.width, message.step = 2, 1, 4
    assert lift2._decode_image(message, 'raw').tolist() == [[[1, 2, 3]], [[4, 5, 6]]]


@pytest.mark.parametrize('field,value', [('step', 5), ('height', 0), ('width', 20_000_000),
                                       ('encoding', '16UC1'), ('data', b'x')])
def test_malformed_raw_images_rejected(lift2, field, value):
    message = raw_image()
    setattr(message, field, value)
    with pytest.raises(ValueError): lift2._decode_image(message, 'raw')


def test_compressed_rgb_decode(lift2):
    data = io.BytesIO()
    Image.new('RGB', (2, 1), (10, 20, 30)).save(data, format='PNG')
    decoded = lift2._decode_image(NS(data=data.getvalue()), 'compressed')
    assert decoded.tolist() == [[[10, 20, 30], [10, 20, 30]]]
    assert not decoded.flags.writeable


def test_camera_error_preserves_stale_preview_but_not_valid_recording(device):
    robot, _, _ = device
    robot._on_image('head', raw_image())
    wait_for(lambda: 'head' in robot.recording_snapshot()['frames'])
    old = robot.recording_snapshot()['frames']['head']
    robot._on_image('head', raw_image(b'x'))
    wait_for(lambda: bool(robot.recording_snapshot()['frames']['head']['error']))
    frame = robot.recording_snapshot()['frames']['head']
    assert not frame['valid'] and frame['sequence'] == old['sequence']
    assert frame['timestamp'] == old['timestamp']
    assert 'head' in robot.camera_preview_snapshot()
    robot._on_image('head', raw_image())
    wait_for(lambda: robot.recording_snapshot()['frames']['head']['valid'])


@pytest.mark.parametrize('config', [
    {}, {'ros_master_uri': 'http://127.0.0.1:11311'},
    {'ros_master_uri': 'not-a-uri', 'ros_ip': '127.0.0.1'},
    {'ros_master_uri': True, 'ros_ip': '127.0.0.1'},
    {'ros_master_uri': 'http://127.0.0.1:11311', 'ros_ip': 1},
    {'ros_master_uri': 'http://127.0.0.1:11311', 'ros_ip': 'robot'},
    {'ros_master_uri': 'http://127.0.0.1:11311', 'ros_ip': '127.0.0.1', 'feedback_timeout_s': True},
])
def test_invalid_connection_config_fails_before_ros_import(lift2, config):
    with pytest.raises(ValueError): lift2.create_connector(config)


def test_connect_cancellation_before_ros_initialization(lift2, monkeypatch):
    monkeypatch.setattr(lift2, '_load_ros', lambda: pytest.fail('ROS should not load'))
    robot = lift2.create_connector({'ros_master_uri': 'http://127.0.0.1:11311', 'ros_ip': '127.0.0.1'})
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeError, match='cancel'): robot.connect(cancel)


def test_close_is_idempotent_and_disables_sends(device):
    robot, pubs, _ = device
    robot.close()
    robot.close()
    assert all(p.closed for p in pubs.values())
    with pytest.raises(RuntimeError, match='closed'): robot.execute_arms({'left': target()})


def test_pose_updates_do_not_rewrite_existing_feedback_history(device):
    robot, _, _ = device
    rows = robot.feedback_since('left', 0)
    pose = eef()
    pose.x = 9.
    robot._on_eef('left', pose)
    assert robot.read_state()['eef'][0] == 9.
    assert robot.feedback_since('left', 0) == rows


def test_ros_initialization_cannot_hide_connect_deadline(lift2, monkeypatch):
    finish = threading.Event()
    binding = NS(rospy=NS(init_node=lambda *a, **k: finish.wait(2), signal_shutdown=lambda reason: None))
    monkeypatch.setattr(lift2, '_load_ros', lambda: binding)
    robot = lift2.create_connector({'ros_master_uri': 'http://127.0.0.1:11311', 'ros_ip': '127.0.0.1',
                                    'connect_timeout_s': .04, 'close_timeout_s': .04})
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match='deadline|timed out'): robot.connect()
        assert time.monotonic()-started < .25
    finally:
        finish.set()
        robot.close()


def test_cleanup_continues_unregistering_after_one_ros_error(device):
    robot, pubs, _ = device
    def fail(): raise RuntimeError('unregister failed')
    pubs['/joint_control'].unregister = fail
    with pytest.raises(RuntimeError, match='unregister failed'): robot.close()
    assert pubs['/joint_control2'].closed
    # Preserve the cleanup failure on repeated close; fixture expects ordinary close.
    with pytest.raises(RuntimeError, match='unregister failed'): robot.close()
    robot._cleanup_error = None


def test_connect_cannot_advertise_ready_before_command_transport_handshake(device, lift2):
    _, _, ros = device
    publisher = ros.Publisher
    def disconnected(*args, **kwargs):
        result = publisher(*args, **kwargs)
        result.connections = 0
        return result
    ros.Publisher = disconnected
    robot = lift2.create_connector({'ros_master_uri': 'http://127.0.0.1:11311', 'ros_ip': '127.0.0.1',
                                    'connect_timeout_s': .04})
    try:
        with pytest.raises(RuntimeError, match='subscriber|deadline'): robot.connect()
    finally:
        robot.close()
