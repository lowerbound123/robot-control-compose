"""Independent recorder acceptance with changing RGB, a real SDK host and local HTTP.

Run with --docker-image for a uniquely labelled, network-isolated container, or
with the current interpreter. No live provider, ROS driver or robot is contacted.
"""
import argparse
import base64
import csv
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid


ROOT = Path(__file__).resolve().parents[2]
CAMERAS = ('head', 'left_wrist', 'right_wrist')
SOURCE = '''
import base64, io, json, urllib.request
from PIL import Image
def initialize(config): return urllib.request.build_opener(urllib.request.ProxyHandler({}))
def policy(context, state, action_chunk, images, config, *, runtime):
    encoded = {}
    for name in ('head', 'left_wrist', 'right_wrist'):
        assert not images[name].flags.writeable
        stream = io.BytesIO()
        Image.fromarray(images[name]).save(stream, format='PNG', compress_level=1)
        encoded[name] = base64.b64encode(stream.getvalue()).decode('ascii')
    body = {'observation_id': runtime.observation_id, 'state': state, 'images_png': encoded}
    request = urllib.request.Request(config['params']['url'], json.dumps(body).encode(),
                                     {'Content-Type': 'application/json'})
    with context.open(request, timeout=5) as response: result = json.load(response)
    assert result['observation_id'] == runtime.observation_id
    return result['actions']
def reset_context(context): pass
def release(context): pass
'''

ROS_SOURCE = '''
import io, json, sys, time
import numpy as np
from PIL import Image
import rclpy
from rclpy.executors import ExternalShutdownException
from robot_control_test_sim.simulator import ArmCameraSimulator
class RecorderSimulator(ArmCameraSimulator):
    def command(self, arm, message):
        super().command(arm, message)
        values = list(message.joint_pos if message.mode == 5 else message.end_pos) + [message.gripper]
        print(json.dumps({'event':'ros.command.received','arm':arm,'mode':'qpos' if message.mode==5 else 'epos',
                          'values':values,'count':self.command_counts[arm], 'monotonic_ns':time.monotonic_ns()}), flush=True)
rclpy.init()
# Existing simulator emits images every third feedback tick: 90 / 3 = 30 Hz.
node = RecorderSimulator(sys.argv[1], frequency_hz=90)
width, height = int(sys.argv[2]), int(sys.argv[3])
for index, camera in enumerate(('head','left_wrist','right_wrist')):
    pixels = np.zeros((height,width,3), dtype=np.uint8)
    pixels[...,index] = 220
    pixels[...,(index+1)%3] = np.arange(width,dtype=np.uint8)
    stream=io.BytesIO(); Image.fromarray(pixels).save(stream,format='PNG')
    node.compressed_frames[camera]=stream.getvalue()
try:
    rclpy.spin(node)
except (KeyboardInterrupt, ExternalShutdownException):
    pass
finally:
    node.destroy_node(); rclpy.try_shutdown()
'''


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def rows(path):
    with Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def distribution(values):
    values = sorted(values)
    if not values:
        return {'count': 0}
    def percentile(fraction):
        return values[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)]
    return {'count': len(values), 'mean': sum(values) / len(values),
            'p50': percentile(.5), 'p95': percentile(.95), 'p99': percentile(.99), 'max': values[-1]}


def source_hashes():
    names = ('src/robot_control/recording.py', 'src/robot_control/recording_writer.py',
             'src/robot_control/robots/mock.py', 'src/robot_control/robots/acone.py', 'src/robot_control/process.py',
             'packages/policy-sdk/src/rc_policy_sdk/host.py',
             'ros_ws/src/robot_control_test_sim/robot_control_test_sim/simulator.py',
             'tests/manual/recording_acceptance.py')
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def docker_run(args, evidence):
    """Only the uniquely named container created here may be removed."""
    deployment = args.deployment_id
    source = evidence / 'source'
    for name in ('src', 'packages/policy-sdk/src', 'ros_ws/src/robot_control_test_sim'):
        shutil.copytree(ROOT / name, source / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', '.DS_Store'))
    (source / 'tests/manual').mkdir(parents=True)
    shutil.copy2(__file__, source / 'tests/manual/recording_acceptance.py')
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.docker_image], text=True))[0]
    name = 'rc-record-' + deployment
    command = ['docker', 'run', '--rm', '--pull=never', '--name', name,
               '--label', 'io.robot-control.deployment=' + deployment,
               '--label', 'io.robot-control.acceptance=recording', '--network', 'none',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--user', f'{os.getuid()}:{os.getgid()}', '--shm-size', '512m',
               '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m',
               '--mount', f'type=bind,source={source},target=/workspace,readonly',
               '--mount', f'type=bind,source={evidence},target=/evidence',
               '--workdir', '/workspace', '--env', 'PYTHONDONTWRITEBYTECODE=1',
               '--env', 'PYTHONPATH=/workspace/src:/workspace/packages/policy-sdk/src:/workspace/ros_ws/src/robot_control_test_sim']
    if args.ros:
        command += ['--env', 'ROS_LOCALHOST_ONLY=1', '--env', 'ROS_DOMAIN_ID=' + str(10 + int(deployment[:2], 36) % 190),
                    '--entrypoint', '/bin/bash', args.docker_image, '-lc',
                    'source /opt/ros/humble/setup.bash && source /opt/rc_ros_ws/install/setup.bash && exec "$@"',
                    '--', 'python3']
    else:
        command += ['--entrypoint', 'python3', args.docker_image]
    command += [
               '/workspace/tests/manual/recording_acceptance.py',
               '--evidence', '/evidence', '--deployment-id', deployment,
               '--duration', str(args.duration), '--width', str(args.width), '--height', str(args.height),
               '--control-hz', str(args.control_hz), '--model-interval', str(args.model_interval),
               '--model-delay', str(args.model_delay), '--max-gap-ratio', str(args.max_gap_ratio),
               '--sample-budget-mib', str(args.sample_budget_mib), '--pattern', args.pattern]
    if args.smoke:
        command.append('--smoke')
    if args.ros:
        command.append('--ros')
    dump(evidence / 'deployment.json', {'deployment_id': deployment, 'container_name': name,
         'image': args.docker_image, 'image_id': image['Id'], 'network': 'none', 'command': command})
    code = 1
    try:
        with (evidence / 'container.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=args.duration + 120)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(5)
                raise RuntimeError('acceptance container exceeded its absolute deadline')
    finally:
        owned = subprocess.check_output(['docker', 'ps', '-aq', '--filter',
                  'label=io.robot-control.deployment=' + deployment], text=True).split()
        if owned:
            subprocess.run(['docker', 'rm', '-f', *owned], check=True, stdout=subprocess.DEVNULL)
            code = 1
        remaining = subprocess.check_output(['docker', 'ps', '-aq', '--filter',
                      'label=io.robot-control.deployment=' + deployment], text=True).split()
        dump(evidence / 'teardown.json', {'deployment_id': deployment, 'containers_remaining': remaining,
                                        'forced_container_removal': bool(owned)})
        if remaining:
            code = 1
    print((evidence / 'container.log').read_text(), end='', flush=True)
    print('Evidence:', evidence, flush=True)
    return code


def make_robot(width, height, pattern):
    import numpy as np
    from robot_control.robots import MockRobot

    class StreamingMockRobot(MockRobot):
        """Test-owned sensor thread; recorder calls still only read cached frames."""
        def connect(self):
            super().connect()
            self.camera_stop = threading.Event()
            self.camera_frames = {}
            self.sensor_ticks = 0
            self.bases = dict(self._images)
            self.publish_camera(0)
            self.camera_thread = threading.Thread(target=self.camera_loop, name='acceptance-camera', daemon=True)
            self.camera_thread.start()

        def publish_camera(self, tick):
            frames = {}
            for index, camera in enumerate(CAMERAS):
                if pattern == 'noise':
                    pixels = np.random.default_rng(tick * 3 + index).integers(0, 256, (height, width, 3), dtype=np.uint8)
                else:
                    pixels = self.bases[camera].copy()
                    # A changing full-height strip forces fresh PNG encoding;
                    # explicitly synthetic and compressible, never a camera benchmark.
                    column = (tick * 11 + index * 17) % width
                    pixels[:, column: min(width, column + 8), :] = (tick + index * 71) % 256
                    pixels[0, :min(width, 8), :] = (tick % 256, (tick // 256) % 256, index)
                pixels.flags.writeable = False
                frames[camera] = {'image': pixels, 'sequence': tick + 1,
                    'timestamp': self._camera_wall_epoch + tick / 30,
                    'received': self._camera_epoch + tick / 30}
            with self._lock:
                self.camera_frames = frames
                self.sensor_ticks += 1

        def camera_loop(self):
            previous = 0
            while not self.camera_stop.is_set():
                tick = max(0, int((time.monotonic() - self._camera_epoch) * 30))
                if tick > previous:
                    self.publish_camera(tick)
                    previous = tick
                self.camera_stop.wait(max(.0001, min(.01, self._camera_epoch + (tick + 1) / 30 - time.monotonic())))

        def _camera_snapshot_locked(self):
            return {name: dict(frame) for name, frame in self.camera_frames.items()}

        def close(self):
            self.camera_stop.set()
            self.camera_thread.join(2)
            if self.camera_thread.is_alive():
                raise RuntimeError('acceptance camera did not exit')
            super().close()

    return StreamingMockRobot({'image_size': (height, width), 'initial_qpos': [0.] * 14})


def make_ros_robot(args, evidence):
    from robot_control.robots import ACOneRobot
    os.environ['ROS_LOCALHOST_ONLY'] = '1'
    os.environ['ROS_LOG_DIR'] = str(evidence / 'ros-logs')
    os.environ.setdefault('ROS_DOMAIN_ID', str(10 + int(args.deployment_id[:2], 36) % 190))
    namespace = '/recording_acceptance/run_' + args.deployment_id
    topic = lambda suffix: namespace + '/' + suffix
    source = evidence / 'ros-simulator.py'
    source.write_text(ROS_SOURCE)
    log = (evidence / 'ros-simulator.log').open('w', encoding='utf-8')
    process = subprocess.Popen([sys.executable, str(source), namespace, str(args.width), str(args.height)],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    robot = ACOneRobot({'namespace': namespace, 'message_type': 'robot_control_test_msgs/msg/ArmState',
        'command_message_type': 'robot_control_test_msgs/msg/ArmCommand',
        'feedback_left_topic': topic('feedback/left'), 'feedback_right_topic': topic('feedback/right'),
        'command_left_topic': topic('command/left'), 'command_right_topic': topic('command/right'),
        'camera_topics': {camera: topic('camera/' + camera + '/compressed') for camera in CAMERAS},
        'connect_timeout_s': 15., 'feedback_timeout_s': 1., 'camera_timeout_s': 1.})
    try:
        robot.connect()
        deadline = time.monotonic() + 5
        while len(robot.recording_snapshot()['frames']) != 3:
            if time.monotonic() >= deadline:
                raise RuntimeError('ROS simulator did not publish three cameras')
            time.sleep(.01)
    except BaseException:
        robot.close()
        os.killpg(process.pid, signal.SIGTERM); process.wait(5); log.close()
        raise
    return robot, process, log


def model_server(args):
    import numpy as np
    from PIL import Image

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                if self.path != '/infer':
                    raise ValueError('unexpected model route')
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if set(request['images_png']) != set(CAMERAS):
                    raise ValueError('model did not receive all cameras')
                hashes = {}
                for camera, encoded in request['images_png'].items():
                    with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True))) as image:
                        if image.mode != 'RGB' or image.size != (args.width, args.height):
                            raise ValueError('model received unexpected RGB dimensions')
                        hashes[camera] = hashlib.sha256(np.asarray(image).tobytes()).hexdigest()
                time.sleep(args.model_delay)
                actions = [[round(value + .001, 6) for value in request['state']['qpos']]]
                self.server.requests.append({'observation_id': request['observation_id'],
                    'camera_pixel_sha256': hashes, 'actions': actions, 'timestamp_ns': time.time_ns()})
                payload = {'observation_id': request['observation_id'], 'actions': actions}
                status = 200
            except Exception as exc:
                self.server.errors.append(str(exc))
                payload, status = {'error': str(exc)}, 422
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    server.requests, server.errors = [], []
    thread = threading.Thread(target=server.serve_forever, name='acceptance-http', daemon=True)
    thread.start()
    return server, thread


def start_policy(evidence, url, recorder):
    from robot_control.process import PolicyProcess
    temporary = tempfile.TemporaryDirectory(prefix='rr-', dir='/tmp')
    root = Path(temporary.name)
    source = root / 'record_model'
    source.mkdir()
    (source / 'main.py').write_text(SOURCE)
    config = {'schema_version': 3, 'id': 'record_model', 'execution': {
        'mode': 'async', 'call_every_actions': 1, 'control_mode': 'qpos'}, 'params': {'url': url}}
    (source / 'policy.yaml').write_text(json.dumps(config))
    endpoint = root / 'host.sock'
    log = (evidence / 'policy-host.log').open('w', encoding='utf-8')
    host = subprocess.Popen([sys.executable, '-m', 'rc_policy_sdk.host', '--socket', str(endpoint),
          '--policy-root', str(root), '--host-id', 'record_model'], stdout=log, stderr=subprocess.STDOUT)
    worker = PolicyProcess(source, config, host_socket=str(endpoint), init_timeout_s=10,
        inference_timeout_s=6, release_timeout_s=2, on_trace=recorder.policy_event,
        on_log=lambda message: recorder.log({'message': '[record_model] ' + message, 'time': time.time()}))
    try:
        deadline = time.monotonic() + 10
        while not endpoint.exists():
            if host.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('acceptance SDK host failed to listen; inspect policy-host.log')
            time.sleep(.01)
        worker.start()
    except BaseException:
        host.terminate(); host.wait(5); log.close(); temporary.cleanup()
        raise
    return worker, host, log, temporary


def verify_artifacts(folder, model_requests, expected_published, measured_s, args):
    import numpy as np
    from PIL import Image
    commits, states = rows(folder / 'samples.jsonl'), rows(folder / 'state.jsonl')
    controls, policy = rows(folder / 'control.jsonl'), rows(folder / 'policy.jsonl')
    errors = []
    def check(condition, message):
        if not condition:
            errors.append(message)
    tables = {}
    for name in ('epos', 'qpos', 'control_epos', 'control_qpos'):
        with (folder / (name + '.csv')).open(newline='') as stream:
            tables[name] = list(csv.reader(stream))[1:]
        check(len(tables[name]) == len(commits), name + ' row count differs from sample commits')
    check(len(states) == len(commits), 'state count differs from sample commits')
    check([row['sample_id'] for row in commits] == list(range(len(commits))), 'sample IDs are not contiguous')
    published = [event for event in controls if event['event'] == 'control.published']
    check(len(published) == expected_published, 'successful arm publish events are missing')
    check(all(len(event['values']) == 7 for event in published), 'published target shape differs')
    by_publish = {(event['arm'], event['monotonic_ns'], event['attempt_id']): event for event in published}
    selected = set()
    late_observers = 0
    previous = commits[0]['scheduled_ns'] if commits else 0
    for index, commit in enumerate(commits):
        latest = commit.get('control_events', {})
        check(not commit['missed'] or not latest, f'missed sample {index} contains action references')
        for arm, reference in latest.items():
            identity = (arm, reference.get('monotonic_ns'), reference.get('attempt_id'))
            event = by_publish.get(identity, {})
            check(bool(event) and all(reference.get(key) == event.get(key) for key in ('mode', 'values')),
                  f'sample {index} committed {arm} identity is not an actual successful publish')
            check(identity not in selected, f'sample {index} repeats an already sampled action')
            check(reference['monotonic_ns'] <= commit['monotonic_ns'], f'sample {index} references a future action')
            check(reference.get('late_observer') == (reference['monotonic_ns'] <= previous),
                  f'sample {index} late-observer marker disagrees with original publication time')
            selected.add(identity)
            late_observers += bool(reference.get('late_observer'))
        if not commit['missed']:
            previous = commit['monotonic_ns']
        for mode in ('epos', 'qpos'):
            expected = []
            for arm in ('left', 'right'):
                event = latest.get(arm)
                expected += [str(value) for value in event['values']] if event and event['mode'] == mode else [''] * 7
            if index < len(tables['control_' + mode]):
                check(tables['control_' + mode][index][3:] == expected,
                      f'control_{mode} sample {index} does not match last successful arm publishes')
        if index < len(states):
            state = states[index].get('state') or {}
            for table, field in (('qpos', 'qpos'), ('epos', 'eef')):
                expected = [str(value) for value in state.get(field, [''] * 14)]
                if index < len(tables[table]):
                    check(tables[table][index][3:] == expected, f'{table} sample {index} differs from cached state')
    cameras = {}
    verified_assets = {}
    for camera in CAMERAS:
        frames = [commit['images'][camera] for commit in commits]
        present = [frame for frame in frames if frame.get('file')]
        sequences = [frame['sequence'] for frame in present]
        for frame in present:
            path = folder / frame['file']
            check(path.is_file(), 'committed PNG is absent: ' + frame['file'])
            # Every unique lossless asset is decoded and hashed once. Sample hard
            # links are still checked individually, without multiplying RAM use.
            key = frame['sha256']
            if key not in verified_assets and path.is_file():
                with Image.open(path) as picture:
                    array = np.asarray(picture)
                    digest = hashlib.sha256(str((array.shape, str(array.dtype))).encode())
                    digest.update(memoryview(array).cast('B'))
                    check(digest.hexdigest() == key, 'lossless PNG pixel hash differs: ' + frame['file'])
                    check(picture.size == (args.width, args.height) and picture.mode == 'RGB', 'saved RGB dimensions differ')
                    verified_assets[key] = hashlib.sha256(array.tobytes()).hexdigest()
        cameras[camera] = {'written': len(present), 'written_hz': len(present) / measured_s,
            'missing': len(frames) - len(present), 'repeated': sum(bool(frame.get('repeated')) for frame in present),
            'source_sequence_gaps': sum(max(0, b - a - 1) for a, b in zip(sequences, sequences[1:])),
            'unique_assets': len({frame['sha256'] for frame in present})}
        check(len(present) / measured_s >= 29., camera + ' actual valid frame rate is below 29 Hz')
    observations = {event['observation_id']: event for event in policy if event['event'] == 'policy.observation'}
    starts = [event for event in policy if event['event'] == 'policy.start']
    results = [event for event in policy if event['event'] == 'policy.result']
    check(len(model_requests) == len(starts) == len(results), 'HTTP calls and policy start/result counts differ')
    check(bool(model_requests), 'local HTTP model was never called')
    for request in model_requests:
        observation = observations.get(request['observation_id'], {})
        check(bool(observation), 'model input observation is absent')
        for camera, expected in request['camera_pixel_sha256'].items():
            frame = observation.get('image_files', {}).get(camera)
            if not frame:
                check(False, 'model input camera asset is absent: ' + camera)
                continue
            with Image.open(folder / frame['file']) as picture:
                actual = hashlib.sha256(np.asarray(picture).tobytes()).hexdigest()
            check(actual == expected, 'saved model input pixels differ for ' + camera)
    missed = sum(bool(commit['missed']) for commit in commits)
    check(missed / max(1, len(commits)) <= args.max_gap_ratio, 'sampler gap ratio exceeds configured threshold')
    return {'sample_count': len(commits), 'sample_hz': len(commits) / measured_s, 'missed_samples': missed,
            'cameras': cameras, 'published_events': len(published), 'policy_starts': len(starts),
            'policy_results': len(results), 'model_calls': len(model_requests),
            'late_observers': late_observers,
            'verified_unique_png_assets': len(verified_assets), 'verification_errors': errors[:100],
            'verification_error_count': len(errors)}


def run_local(args, evidence):
    from rc_policy_sdk.images import ImagePool
    from robot_control.recording import RecordingService
    simulator = simulator_log = None
    if args.ros:
        robot, simulator, simulator_log = make_ros_robot(args, evidence)
    else:
        robot = make_robot(args.width, args.height, args.pattern)
        robot.connect()
    recorder_errors = []
    recorder = RecordingService(evidence / 'recording', args.deployment_id, robot.recording_snapshot,
        on_error=recorder_errors.append, sample_budget=args.sample_budget_mib * 1024 * 1024)
    server = server_thread = worker = host = host_log = temporary = pool = lease = None
    report = {'deployment_id': args.deployment_id, 'mode': 'smoke' if args.smoke else 'acceptance',
              'platform': platform.platform(), 'python': sys.version, 'source_sha256': source_hashes(),
              'requested_duration_s': args.duration, 'width': args.width, 'height': args.height,
              'pattern': 'ros-static-png' if args.ros else args.pattern, 'robot_backend': 'acone_ros' if args.ros else 'mock',
              'camera_hz': 30, 'control_hz': args.control_hz,
              'model_delay_s': args.model_delay, 'verdict': 'failed'}
    control_ms, total_ms, lateness_ms, statuses = [], [], [], []
    observed = [0]
    forced = []
    try:
        recorder.start()
        server, server_thread = model_server(args)
        worker, host, host_log, temporary = start_policy(evidence, f'http://127.0.0.1:{server.server_port}/infer', recorder)
        pool = ImagePool(cameras=CAMERAS)
        def on_publish(event):
            observed[0] += 1
            recorder.sent(event)
        robot.set_command_observer(on_publish)
        metadata = {'deployment_id': args.deployment_id, 'fixture': 'recording-acceptance',
                    'source_sha256': report['source_sha256'], 'robot': {'backend': report['robot_backend'], 'image_size': [args.height, args.width]},
                    'policy': worker.config, 'recording_hz': 30}
        recorder.command('on', metadata).result(10)
        start = time.monotonic()
        deadline = start + args.duration
        scheduled = start
        next_model = start
        next_status = start
        next_progress = start + 10
        request_id = 0
        target = [0.] * 14
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now < scheduled:
                time.sleep(min(scheduled - now, deadline - now))
                continue
            lateness_ms.append(max(0., now - scheduled) * 1000)
            scheduled += 1 / args.control_hz
            # A missed control period is reported, never replayed in a burst.
            if scheduled < now:
                scheduled = now + 1 / args.control_hz
            robot.read_state()
            mode = 'qpos' if int(now - start) % 4 < 2 else 'epos'
            targets = {arm: {'mode': mode, 'values': target[index * 7:index * 7 + 7]}
                       for index, arm in enumerate(('left', 'right'))}
            before = time.perf_counter_ns()
            recorder.control_attempt({'targets': targets, 'generation': 1, 'control_owner': 'fixture'})
            execute_started = time.perf_counter_ns()
            robot.execute_arms(targets)
            after = time.perf_counter_ns()
            control_ms.append((after - execute_started) / 1e6)
            total_ms.append((after - before) / 1e6)
            response = worker.poll()
            if response:
                if response['type'] != 'result' or response.get('images_released') is not True:
                    raise RuntimeError('model policy failed: ' + str(response))
                target = response['actions'][-1]
                lease.release(); lease = None
            if lease is None and now >= next_model and now + args.model_delay + .2 < deadline:
                request_id += 1
                observation_id = args.deployment_id + ':' + str(request_id)
                capture = robot.recording_snapshot()
                images = {name: (frame['image'], frame['timestamp']) for name, frame in capture['frames'].items()}
                if not recorder.observation(observation_id, capture['state'], images):
                    raise RuntimeError('model observation enqueue rejected')
                for name, (pixels, timestamp) in images.items():
                    if not pool.publish(name, pixels, timestamp):
                        raise RuntimeError('policy image pool is busy')
                lease = pool.acquire()
                worker.submit({'request_id': request_id, 'generation': 1, 'config_version': 1,
                    'state': capture['state'], 'action_chunk': [], 'images': lease.descriptors,
                    'runtime': {'observation_id': observation_id, 'consumed_actions': len(control_ms)}})
                next_model = now + args.model_interval
            if now >= next_status:
                status = recorder.status()
                statuses.append({'elapsed_s': now - start, **status})
                if status['state'] != 'recording':
                    raise RuntimeError('recorder stopped during workload: ' + str(status))
                next_status = now + .1
            if now >= next_progress:
                print(json.dumps({'elapsed_s': round(now - start, 2), 'status': recorder.status()}), flush=True)
                next_progress += 10
        end = time.monotonic()
        report['measured_duration_s'] = end - start
        if lease is not None:
            drain = time.monotonic() + 6
            while lease is not None and time.monotonic() < drain:
                response = worker.poll()
                if response:
                    if response['type'] != 'result' or response.get('images_released') is not True:
                        raise RuntimeError('last model inference failed: ' + str(response))
                    lease.release(); lease = None
                else:
                    time.sleep(.005)
            if lease is not None:
                raise RuntimeError('last model inference did not release images')
        final_status = recorder.command('off').result(35)
        folder = Path(final_status['path'])
        report.update(recording_folder=str(folder.relative_to(evidence)), final_status=final_status,
                      control_execute_ms=distribution(control_ms), control_attempt_and_execute_ms=distribution(total_ms),
                      control_start_lateness_ms=distribution(lateness_ms), actual_control_hz=len(control_ms) / (end - start),
                      max_pending_samples=max((status['pending_samples'] for status in statuses), default=0),
                      peak_pending_bytes=final_status['peak_pending_bytes'], sensor_ticks=getattr(robot, 'sensor_ticks', None))
        report.update(verify_artifacts(folder, server.requests, observed[0], end - start, args))
        if final_status['error'] or final_status['extra_dropped'] or recorder_errors or server.errors:
            report['verification_errors'].append('recorder/model reported errors or dropped optional events')
            report['verification_error_count'] += 1
        report['verdict'] = 'passed' if report['verification_error_count'] == 0 else 'failed'
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        report['traceback'] = traceback.format_exc()
        report['status_at_error'] = recorder.status()
    finally:
        robot.set_command_observer(None)
        if worker is not None:
            try:
                forced.append(bool(worker.close(deadline=time.monotonic() + 10)))
            except Exception as exc:
                report['worker_cleanup_error'] = str(exc)
                report['verdict'] = 'failed'
        if host is not None:
            host.terminate()
            try:
                host.wait(5)
            except subprocess.TimeoutExpired:
                host.kill(); host.wait(2); forced.append(True)
            host_log.close()
        if lease is not None and worker is not None and worker.cleanup_confirmed:
            lease.release()
        if pool is not None:
            pool.close()
        if server is not None:
            server.shutdown(); server.server_close(); server_thread.join(2)
            dump(evidence / 'model-requests.json', server.requests)
            report['model_errors'] = server.errors
        if temporary is not None:
            temporary.cleanup()
        forced.append(bool(recorder.shutdown(time.monotonic() + 35)))
        robot.close()
        if simulator is not None:
            os.killpg(simulator.pid, signal.SIGTERM)
            try:
                simulator.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(simulator.pid, signal.SIGKILL); simulator.wait(2); forced.append(True)
            simulator_log.close()
            received = []
            for line in (evidence / 'ros-simulator.log').read_text().splitlines():
                if line.startswith('{'):
                    event = json.loads(line)
                    if event.get('event') == 'ros.command.received':
                        received.append(event)
            report['ros_commands_received'] = len(received)
            published = rows(Path(report['final_status']['path']) / 'control.jsonl') if 'final_status' in report else []
            published = [event for event in published if event.get('event') == 'control.published']
            for arm in ('left', 'right'):
                expected = [(event['mode'], event['values']) for event in published if event['arm'] == arm]
                actual = [(event['mode'], event['values']) for event in received if event['arm'] == arm]
                if actual != expected:
                    report['ros_receipt_error'] = arm + ' ROS received commands differ from successful publishes'
                    report['verdict'] = 'failed'
        report['forced_cleanup'] = any(forced)
        if report['forced_cleanup']:
            report['verdict'] = 'failed'
        report['recorder_errors'] = recorder_errors
        dump(evidence / 'status-history.json', statuses)
        dump(evidence / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report['verdict'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=60.)
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=480)
    parser.add_argument('--control-hz', type=float, default=50.)
    parser.add_argument('--model-interval', type=float, default=1.)
    parser.add_argument('--model-delay', type=float, default=.05)
    parser.add_argument('--sample-budget-mib', type=int, default=512)
    parser.add_argument('--pattern', choices=('moving', 'noise'), default='moving')
    parser.add_argument('--max-gap-ratio', type=float, default=0.)
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--deployment-id', default=uuid.uuid4().hex[:16])
    parser.add_argument('--docker-image', help='existing RC test image; no image pulls or builds')
    parser.add_argument('--ros', action='store_true', help='use real ACOne ROS transport and the test simulator')
    parser.add_argument('--smoke', action='store_true', help='allow durations below 60 seconds, reported only as smoke')
    args = parser.parse_args()
    numbers = (args.duration, args.control_hz, args.model_interval, args.sample_budget_mib)
    if any(not math.isfinite(value) or value <= 0 for value in numbers):
        parser.error('duration, control-hz, model-interval and sample-budget-mib must be positive finite values')
    if args.duration < 60 and not args.smoke:
        parser.error('acceptance requires --duration >= 60; use --smoke only for a short fixture check')
    if not (1 <= args.width <= 4096 and 1 <= args.height <= 4096):
        parser.error('image dimensions must be within 1..4096')
    if not (0 <= args.model_delay < 4 and 0 <= args.max_gap_ratio <= 1):
        parser.error('model-delay must be within [0,4), max-gap-ratio within [0,1]')
    if not args.deployment_id.isalnum() or len(args.deployment_id) > 40:
        parser.error('deployment-id must be 1..40 letters/digits')
    evidence = args.evidence or Path(tempfile.gettempdir()) / ('rc-record-acceptance-' + args.deployment_id)
    evidence = evidence.expanduser().resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    if (evidence / 'report.json').exists() or (args.docker_image and (evidence / 'source').exists()):
        parser.error('evidence already contains a run; choose a new directory')
    if args.docker_image:
        return docker_run(args, evidence)
    try:
        return run_local(args, evidence)
    except Exception as exc:
        # Bootstrap failures (e.g. unavailable ROS bindings) must leave a report
        # too, even before the recorder or simulator can reach their main loop.
        report = {'deployment_id': args.deployment_id, 'verdict': 'failed',
                  'error': f'{type(exc).__name__}: {exc}', 'traceback': traceback.format_exc()}
        dump(evidence / 'report.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
