"""Measure real RC -> child connector cadence, receipts and shared images (no hardware)."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
for path in ('src', 'tests', 'packages/policy-sdk/src', 'packages/connector-sdk/src'):
    sys.path.insert(0, str(ROOT / path))

import numpy as np
from config_helpers import read_config, connector_config
from host_helpers import LocalPolicyProcess, cleanup_hosts
from project_templates import copy_templates
from robot_control.controller import RobotController


def wait(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline: raise TimeoutError('connector acceptance timeout')
        time.sleep(.005)


def measure(hz, duration, recording):
    with tempfile.TemporaryDirectory(prefix='rc-connector-cadence-', dir='/tmp') as folder:
        root = Path(folder)
        copy_templates(ROOT / 'policy/example', root / 'policy/example')
        manifest = read_config(root / 'policy/example/policy.yaml')
        manifest['execution'].update(mode='async', call_every_actions=4)
        manifest['params']['chunk_size'] = 16
        (root / 'policy/example/policy.yaml').write_text(json.dumps(manifest))
        config = read_config(ROOT / 'configs/rc.mock.yaml.example')
        config.update(frequency_hz=hz, policy_directory='policy', outputs_directory='outputs', startup_policy='example')
        device = root / 'device.yaml'
        device.write_text(json.dumps({'image_size': [480, 640]}))
        connector_config(config, device=device)
        path = root / 'rc.yaml'
        path.write_text(json.dumps(config))
        rc = RobotController(path, process_factory=LocalPolicyProcess)
        times, outstanding, latency, depths = [], {}, [], []
        started = None
        rc.start()
        try:
            wait(lambda: rc.status()['state'] in ('paused', 'fault'))
            assert rc.status()['state'] == 'paused', rc.read_events()
            client = rc.robot.client
            submit, poll = client.submit, client.poll_results
            def measured_submit(targets):
                before = time.monotonic()
                identity = submit(targets)
                if started is not None:
                    times.append(before)
                    outstanding[identity] = before
                    depths.append(len(client._pending))
                return identity
            def measured_poll():
                rows = poll()
                now = time.monotonic()
                for row in rows:
                    before = outstanding.pop(row['id'], None)
                    if before is not None: latency.append(now - before)
                return rows
            client.submit, client.poll_results = measured_submit, measured_poll
            if recording: assert rc.wait_operation(rc.submit('record start'), 5)['status'] == 'succeeded'
            assert rc.wait_operation(rc.submit('resume'), 5)['status'] == 'succeeded'
            wait(lambda: rc.buffer.consumed >= 4)
            started = time.monotonic()
            while time.monotonic() - started < duration:
                assert rc.status()['state'] == 'running', rc.read_events()
                time.sleep(.02)
            elapsed = time.monotonic() - started
            started = None
            report = {'configured_hz': hz, 'duration_s': elapsed, 'submissions': len(times),
                      'thread_switch_interval_s': sys.getswitchinterval(),
                      'measured_hz': (len(times)-1)/(times[-1]-times[0]),
                      'interval_ms': dict(zip(('min', 'p50', 'p95', 'p99', 'max'),
                                             np.percentile(np.diff(times)*1000, [0, 50, 95, 99, 100]).tolist())),
                      'receipt_ms': dict(zip(('p50', 'p95', 'p99', 'max'), np.percentile(np.array(latency)*1000, [50, 95, 99, 100]).tolist())),
                      'peak_pending': max(depths), 'consumed': rc.buffer.consumed,
                      'connector_pid': rc.robot.process.pid,
                      'images': {name: list(frame.shape) for name, (frame, _) in rc.robot.read_images().items()}}
            if recording:
                report['recording'] = rc.wait_operation(rc.submit('record stop'), 10)['result']
                assert report['recording']['state'] == 'idle' and report['recording']['error'] is None
            return report
        finally:
            rc.submit('exit')
            assert rc.done.wait(15), rc.status()
            assert rc.exit_code == 0 and rc.robot.process.poll() == 0 and rc._pool._bytes == 0, rc.read_events()
            cleanup_hosts()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=float, default=30)
    parser.add_argument('--record', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results = [measure(hz, args.duration, args.record) for hz in (30, 100)]
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(results, ensure_ascii=False, indent=2))
