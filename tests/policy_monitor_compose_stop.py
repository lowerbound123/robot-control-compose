"""Stop a real Compose stack while a monitor holds context; no robot or model."""
import json
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import time
import uuid


def inside():
    from robot_control.controller import RobotController
    config = json.loads(Path("/opt/app/configs/rc.mock.json").read_text())
    config.update(policy_directory="/workspace/policy", startup_policy="C", outputs_directory="/tmp/monitor-stop-output")
    config["timeouts"] = {"initialize_s":10, "inference_s":10, "release_s":.3}
    path = Path("/tmp/monitor-stop-rc.json")
    path.write_text(json.dumps(config))
    controller = RobotController(path)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: controller.submit("exit"))
    controller.start()
    worker = None
    deadline = time.monotonic() + 30
    while not controller.done.wait(.05):
        if worker is None and controller.status()["state"] == "paused":
            worker = controller._worker
            controller.policy_monitors.set_enabled("C", True)
        while True:
            try: event = controller.events.get_nowait()
            except queue.Empty: break
            if "monitor-stop-entered" in event["message"]:
                print("monitor-stop-entered", flush=True)
        if time.monotonic() > deadline:
            controller.submit("exit")
    print("monitor-stop-final " + json.dumps({"exit_code":controller.exit_code,
          "pool_bytes":controller._pool._bytes, "readers":worker.has_readers if worker else None}), flush=True)
    return controller.exit_code


def outside():
    root = Path(__file__).resolve().parents[1]
    project = "rc-monitor-stop-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="rc-monitor-stop-") as directory:
        override = Path(directory)/"stop.yaml"
        override.write_text('services:\n  integration:\n    command: ["python3", "/opt/app/tests/policy_monitor_compose_stop.py", "--inside"]\n    stop_grace_period: 15s\n')
        command = ["docker", "compose", "-p", project]
        for path in (root/"compose.yaml", root/"compose.test.yaml", root/"compose.monitor-test.yaml", override):
            command += ["-f", str(path)]
        def run(*args):
            return subprocess.run([*command, *args], cwd=root, text=True, capture_output=True, check=True).stdout
        try:
            run("up", "-d", "integration")
            deadline = time.monotonic() + 25
            while "monitor-stop-entered" not in run("logs", "--no-color", "integration"):
                if time.monotonic() >= deadline: raise AssertionError("monitor callback did not enter")
                time.sleep(.2)
            start = time.monotonic()
            run("stop")
            elapsed = time.monotonic() - start
            container = run("ps", "-a", "-q", "integration").strip()
            state = json.loads(subprocess.check_output(["docker", "inspect", "--format", "{{json .State}}", container], text=True))
            logs = run("logs", "--no-color", "integration")
            assert state["ExitCode"] == 1, state
            assert '"pool_bytes": 0' in logs and '"readers": false' in logs, logs
            assert elapsed < 15, elapsed
            print(f"PASS {project}: Compose stop {elapsed:.2f}s; forced cleanup exit=1, readers=false, pool_bytes=0")
        finally:
            run("down", "-v", "--remove-orphans")


if __name__ == "__main__":
    if "--inside" in sys.argv:
        raise SystemExit(inside())
    outside()
