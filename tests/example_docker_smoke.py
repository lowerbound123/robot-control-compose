"""Isolated live Docker acceptance for the shipped example (never real hardware).

After preparing RC, anchor, hold and example images:
    python3 tests/example_docker_smoke.py --evidence-dir /tmp/example-evidence
"""
from config_helpers import read_config
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def wait(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError("example acceptance condition timed out")


def in_container():
    from robot_control.controller import RobotController
    from robot_control.terminal_commands import TerminalCommandRouter

    root = Path("/workspace")
    report = {"rc_python": platform.python_version(), "checks": []}
    controller = RobotController(root / "configs/rc.mock.yaml")
    router = TerminalCommandRouter(controller)
    router.panel_handler = lambda name, desired: controller.policy_monitors.set_enabled(
        name.removeprefix("monitor-"), desired)

    def command(line):
        identity = router.submit(line)
        result = wait(lambda: (value if value["status"] in ("succeeded", "failed", "cancelled") else None)
                      if (value := controller.get_operation(identity)) else None)
        assert result["status"] == "succeeded", result
        return result.get("result")

    def containers():
        return controller.runtime._ids({"io.robot-control.deployment": controller.runtime.context["deployment_id"]},
                                       time.monotonic() + 10)

    controller.start()
    try:
        wait(lambda: controller._connected and controller._operation is None)
        assert controller.status()["policy"] is None and not containers()
        assert "policy example example" in command("help example")
        command("switch example")
        assert controller.status()["state"] == "paused"
        first = containers()
        assert len(first) == 1
        initial = command("example")
        assert initial["python"].startswith("3.14.") and initial["calls"] == 0
        assert command("policy example example") == initial
        report["example_python"] = initial["python"]
        report["checks"].append("discovery, static help, own Python3.14 container, paused startup, both command forms")

        router.submit("monitor-example on")
        hub = controller.policy_monitors
        wait(lambda: hub.snapshot().get("example", {}).get("values"))
        command("resume")
        wait(lambda: controller.status()["consumed"] >= 3)
        running = command("example")
        assert running["calls"] > 0 and running["observations"] > 0
        images = json.loads(running["images"])
        assert len(images) == 3 and all(value["readonly"] for value in images.values())
        assert images["head"]["sample"] == "180"
        assert images["left_wrist"]["sample"] == images["right_wrist"]["sample"] == "0"
        assert all(value["shape"] == [64, 96, 3] and value["dtype"] == "uint8" for value in images.values())
        assert all(abs(value - .125) < 1e-6 for value in controller.robot.read_state()["qpos"])
        command("pause")
        report["running_snapshot"] = running
        report["checks"].append("resume, current-pose hold, three readonly shared cameras, observe and monitor")

        version = controller.status()["config_version"]
        command('set policy.params.message "容器热更新"')
        command("set policy.params.log_every_n_calls 10")
        assert containers() == first and controller.status()["config_version"] == version + 2
        assert command("example")["message"] == "容器热更新"
        wait(lambda: (hub.snapshot().get("example", {}).get("values") or {}).get("message") == "容器热更新")
        router.submit("monitor-example off")
        assert not hub.snapshot()
        report["checks"].append("hot config visible in commands and monitor without replacing the container")

        command("switch hold_example")
        current = containers()
        assert len(current) == 2 and not set(first) & set(current)
        assert controller.status()["state"] == "paused"
        consumed_before = controller.status()["consumed"]
        command("resume")
        wait(lambda: controller.status()["consumed"] > consumed_before)
        assert command("policy example example")["calls"] > 0
        assert command("example-example")["image_count"] == 3
        assert all(abs(value - .125) < 1e-6 for value in controller.robot.read_state()["qpos"])
        report["checks"].append("hold to example chain uses two containers and retains upstream targets")
    except BaseException:
        print(json.dumps(controller.read_events(), ensure_ascii=False, default=str), flush=True)
        raise
    finally:
        router.close()
        controller.submit("exit")
        assert controller.done.wait(30), controller.status()
        (root / "outputs/example-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    assert controller.exit_code == 0 and controller.robot.closed
    assert not containers() and controller._pool._bytes == 0
    report["checks"].append("clean exit, all policy containers removed, image pool fully released")
    (root / "outputs/example-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)


def on_host(evidence):
    from source_helpers import copy_runtime_sources, anchor_override
    # All generated configuration and deployment identity belong to this temp project.
    from project_templates import copy_templates
    sys.path.insert(0, str(ROOT / "scripts"))
    from setup_project import setup

    evidence.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="rc-example-smoke-"))
    copy_runtime_sources(directory)
    (directory / "configs").mkdir()
    shutil.copyfile(ROOT / ".env.example", directory / ".env.example")
    for identity in ("hold", "example"):
        copy_templates(ROOT / "policy" / identity, directory / "policy" / identity)
        if os.environ.get('RC_TEST_WORKER314_IMAGE'):
            compose_file = directory / 'policy' / identity / 'compose.yaml'
            model = json.loads(subprocess.check_output(['docker','compose','-f',str(compose_file),'config','--format','json']))
            model['services']['policy']['image'] = os.environ['RC_TEST_WORKER314_IMAGE']
            compose_file.write_text(json.dumps(model))
    chain = directory / "policy/hold_example"
    chain.mkdir()
    (chain / "policy.yaml").write_text(json.dumps({
        "schema_version": 3, "kind": "chain", "policies": ["hold", "example"],
    }))
    config = read_config(ROOT / "configs/rc.mock.yaml.example")
    config.update(startup_policy=None, frequency_hz=30)
    config["robot"]["initial_qpos"] = [.125] * 14
    config["timeouts"] = dict(initialize_s=30, inference_s=5, release_s=3, shutdown_s=25)
    (directory / "configs/rc.mock.yaml").write_text(json.dumps(config))
    assert setup(directory, ["hold_example"], config_only=True) == ["hold", "example"]
    context_path = directory / ".setup/runtime.json"
    context = read_config(context_path)
    # Docker Desktop may map the socket to a different GID inside Linux.
    context["docker_gid"] = int(subprocess.check_output([
        "docker", "run", "--rm", "--network", "none", "--entrypoint", "stat",
        "--mount", f"type=bind,source={context['docker_socket']},target=/socket",
        os.environ.get("RC_TEST_WORKER314_IMAGE", "robot-control-compose-example:local"), "-c", "%g", "/socket"], text=True, timeout=30).strip())
    context_path.write_text(json.dumps(context))
    override = {"services": {"robot-control": {
        "command": ["python3", "/validation/example_docker_smoke.py", "--container"],
        "volumes": [
            {"type": "bind", "source": str(ROOT / "tests"), "target": "/validation", "read_only": True},
        ],
    }}}
    anchor_override(override["services"])
    override["services"]["robot-control"]["image"] = os.environ.get("RC_TEST_RC_IMAGE", "robot-control-compose-rc:local")
    (directory / "override.json").write_text(json.dumps(override))
    env = dict(os.environ, RC_BASE_PROJECT=context["base_project"], RC_UID=str(context["uid"]),
               RC_GID=str(context["gid"]), RC_DOCKER_GID=str(context["docker_gid"]),
               RC_DOCKER_SOCKET=context["docker_socket"], RC_RPC_VOLUME=context["rpc_volume"], RC_MCP_PORT="0")
    compose = ["docker", "compose", "--env-file", "/dev/null", "--project-directory", str(directory),
               "-p", context["base_project"], "-f", str(ROOT / "compose.yaml"), "-f", str(directory / "override.json")]
    try:
        subprocess.run(compose + ["up", "-d", "--no-build"], env=env, check=True, timeout=90)
        cid = subprocess.check_output(compose + ["ps", "-aq", "robot-control"], env=env, text=True).strip()
        try:
            code = subprocess.check_output(["docker", "wait", cid], text=True, timeout=180).strip()
        finally:
            logs = subprocess.check_output(["docker", "logs", cid], text=True, stderr=subprocess.STDOUT)
            (evidence / "container.log").write_text(logs)
        output = directory / "outputs/example-smoke.json"
        if output.exists():
            shutil.copyfile(output, evidence / "result.json")
        assert code == "0", logs[-6000:]
        report = read_config(output)
        assert len(report["checks"]) == 5, report
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        # Fallback only touches this task's exact deployment label, never global prune.
        ids = subprocess.check_output(["docker", "ps", "-aq", "--filter",
            "label=io.robot-control.deployment=" + context["deployment_id"]], text=True).split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], check=True, timeout=60)
        subprocess.run(compose + ["down", "-v", "--remove-orphans"], env=env, check=True, timeout=90)
    remaining = subprocess.check_output(["docker", "ps", "-aq", "--filter",
        "label=com.docker.compose.project=" + context["base_project"]], text=True).strip()
    assert not remaining
    assert not subprocess.check_output(["docker", "volume", "ls", "-q", "--filter",
        "label=com.docker.compose.project=" + context["base_project"]], text=True).strip()
    (evidence / "cleanup.json").write_text(json.dumps({"project": context["base_project"], "containers": [], "volumes": []}))
    print("evidence:", evidence)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", action="store_true")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    if args.container:
        in_container()
    else:
        on_host(args.evidence_dir or Path(tempfile.mkdtemp(prefix="rc-example-evidence-")))
