"""SIGTERM while recording uses the controller's normal bounded cleanup."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

from test_controller import setup_controller


def test_sigterm_closes_active_recording_and_reaps_writer(setup_controller, tmp_path):
    controller, _, _ = setup_controller
    controller.close_unstarted()
    script = tmp_path/'signal_recording.py'
    script.write_text('''
import json, sys, time
from robot_control.controller import RobotController
from robot_control.cli import shutdown_signals
from test_controller import FakeProcess, until

if __name__ == '__main__':
    controller = RobotController(sys.argv[1], process_factory=FakeProcess)
    with shutdown_signals(controller):
        controller.start()
        until(lambda: controller.status()['policy'] == 'first')
        operation = controller.submit('record on')
        assert controller.wait_operation(operation, 5)['status'] == 'succeeded'
        until(lambda:controller.status()['recording']['samples_written'] >= 5)
        print(json.dumps({'writer':controller._recorder._process.pid,
                          'path':controller.status()['recording']['path']}), flush=True)
        assert controller.done.wait(8)
        assert not any(t.is_alive() for t in (controller._recorder._manager,
                      controller._recorder._sampler, controller._recorder._feeder))
    sys.exit(controller.exit_code)
''')
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).parent) + os.pathsep + env.get('PYTHONPATH','')
    process = subprocess.Popen([sys.executable, str(script), str(controller.config_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        assert select.select([process.stdout], [], [], 8)[0], 'child did not begin recording'
        line = process.stdout.readline()
        assert line, process.stderr.read()
        ready = json.loads(line)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, (stdout, stderr)
        folder = Path(ready['path'])
        sessions = [json.loads(row) for row in (folder/'session.jsonl').read_text().splitlines()]
        assert any(row['event'] == 'record.stopped' for row in sessions)
        assert len((folder/'qpos.csv').read_text().splitlines()) >= 6
        baseline = next(folder.parent.glob('rc_*.jsonl'))
        final = json.loads(baseline.read_text().splitlines()[-1])
        assert final['event'] == 'rc.exit' and final['writer_drained'] and final['exit_code'] == 0
        try:
            os.kill(ready['writer'], 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError('recording writer survived RC exit')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
