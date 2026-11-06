"""Isolated live Docker acceptance for the shipped example (never real hardware).

After preparing RC, anchor and example images:
    python3 tests/example_docker_smoke.py --evidence-dir /tmp/example-evidence
"""
from config_helpers import read_config
import argparse
import asyncio
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


def mcp_management_checks(controller, report):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from robot_control.mcp_server import MCPService
    from unittest.mock import patch

    service = MCPService(controller, {'host': '127.0.0.1', 'port': 0}, allow_ephemeral=True)
    service.start()
    async def request(name, arguments):
        # Server-side lifecycle waits can legitimately take up to 30 seconds.
        # The transport must not time out first at HTTPX's five-second default.
        async with httpx2.AsyncClient(trust_env=False, timeout=45.) as http:
            async with streamable_http_client(service.url, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return await session.call_tool(name, arguments)
    def call(name, *, fail=False, **arguments):
        reply = asyncio.run(request(name, arguments))
        assert reply.is_error == fail, reply
        return reply.structured_content
    def identities():
        return {name: dict(container=w.endpoint.container_id, host=w.host.instance_id,
                           process=w.runtime_id, pid=w.pid, version=w.config_version)
                for name, w in controller._worker.member_workers().items()}
    def context(instance):
        return call('policy_command', instance_id=instance, command='example', wait=True)['result']
    evidence = {}
    try:
        assert len(call('policy_commands', policy_id='example')['policies']) == 2
        assert call('help', policy_id='example-1', command='reinitialize')['policies'][0]['commands'][0]['effect'] == 'reinitialize_policy'
        call('take', wait=True)
        old = identities()
        call('policy_chain', policies=['example', 'example'], action='save', wait=True)
        assert identities() == old
        call('policy_chain', policies=['example', 'example'], wait=True)
        initial = identities()
        assert not {x['container'] for x in old.values()} & {x['container'] for x in initial.values()}
        assert controller.control_owner == 'mcp' and controller.status()['state'] == 'paused'
        before_context = {i: context(i) for i in initial}
        with patch.object(controller.runtime, 'runner', side_effect=AssertionError('in-place MCP command called Docker')):
            call('policy_command', instance_id='example-1', command='reinitialize-chunk-size', args=[8], wait=True)
        reinitialized = identities()
        after_context = {i: context(i) for i in initial}
        assert reinitialized['example-0'] == initial['example-0']
        assert reinitialized['example-1'] == {**initial['example-1'], 'version': initial['example-1']['version'] + 1}
        assert before_context['example-0']['context_id'] == after_context['example-0']['context_id']
        assert before_context['example-1']['module_id'] == after_context['example-1']['module_id']
        assert before_context['example-1']['context_id'] != after_context['example-1']['context_id']
        call('command', line='policy example-1 set-chunk-size 9', wait=True)
        reloaded = identities()
        assert reloaded['example-0'] == reinitialized['example-0']
        assert reloaded['example-1']['container'] != reinitialized['example-1']['container']
        target = controller._worker.member_workers()['example-1']
        receive = target._receive
        def lost_ack(packet):
            event = packet['event']
            if event['type'] == 'ack' and 'monitor' in event and 'observe' in event:
                packet = {**packet, 'event': {**event, 'type': 'error', 'error': 'MCP acceptance lost initialization ACK'}}
            receive(packet)
        with patch.object(target, '_receive', side_effect=lost_ack):
            call('policy_command', instance_id='example-1', command='reinitialize', wait=True, fail=True)
        assert controller.control_owner == 'mcp' and controller.status()['retry_reload_instance'] == 'example-1'
        assert identities() == reloaded
        call('retry_reload', instance_id='example-1', wait=True)
        retried = identities()
        assert retried['example-0'] == reloaded['example-0']
        assert retried['example-1']['container'] != reloaded['example-1']['container']
        call('policy_command', instance_id='example-1', command='set-chunk-size-all', args=[10], wait=True)
        all_reloaded = identities()
        assert all(all_reloaded[i]['container'] != retried[i]['container'] for i in retried)
        assert controller.control_owner == 'mcp' and controller.status()['state'] == 'paused'
        profiles = controller.root / 'robots'
        profiles.mkdir(exist_ok=True)
        (profiles / 'alternate.yaml').write_text(json.dumps({'backend': 'mock', 'initial_qpos': [.25]*14}))
        call('command', line='set example-0.execution.on_backend_switch "reset"', wait=True)
        backend_before = identities()
        context_before = {i: context(i) for i in backend_before}
        recorded_before = call('record', action='on', wait=True)['result']['session_id']
        with patch.object(controller.runtime, 'runner', side_effect=AssertionError('backend switch replaced a policy container')):
            call('switch_backend', profile='alternate', wait=True)
        backend_after = identities()
        context_after = {i: context(i) for i in backend_before}
        assert backend_after['example-0'] == backend_before['example-0']
        assert backend_after['example-1'] == {**backend_before['example-1'], 'version':backend_before['example-1']['version']+1}
        assert context_after['example-0']['context_id'] == context_before['example-0']['context_id']
        assert context_after['example-1']['context_id'] != context_before['example-1']['context_id']
        assert all(abs(value-.25) < 1e-6 for value in controller.robot.read_state()['qpos'])
        assert controller.status()['recording']['session_id'] != recorded_before
        call('record', action='off', wait=True)
        report['backend_switch'] = dict(before=backend_before, after=backend_after, status=controller.status()['backend'])
        report['checks'].append('backend switch keeps per-instance containers/PIDs, mixes reset/reinitialize and splits recording')
        episode = call('return_and_wait', timeout_s=0)['operation_id']
        wait(lambda: controller.status()['state'] == 'running')
        epoch = controller._control_epoch
        started = call('record', action='on', wait=True)['result']
        wait(lambda: controller.status()['recording']['samples_written'] >= 3)
        stopped = call('command', line='record off', wait=True)['result']
        assert started['session_id'] == stopped['session_id'] and stopped['state'] == 'idle'
        assert controller._control_epoch == epoch and controller.control_owner == 'policy'
        assert not call('get_policy_result', operation_id=episode)['finished']
        completed = controller.submit('example-1-next-result completed mcp-management')
        assert controller.wait_operation(completed, 5)['status'] == 'succeeded'
        assert controller.wait_operation(episode, 10)['status'] == 'succeeded'
        evidence.update(initial=initial, reinitialized=reinitialized, reloaded=reloaded, retried=retried,
                        all_reloaded=all_reloaded, recording=stopped,
                        outcome=call('get_policy_result', operation_id=episode)['result'])
        report['mcp_management'] = evidence
        report['checks'].append('HTTP MCP chain save/apply, discovery, scoped/all/in-place reload, retry, independent recording and episode handback')
    finally:
        service.close()


def in_container():
    from unittest.mock import patch
    from robot_control.controller import RobotController
    from robot_control.terminal_commands import TerminalCommandRouter

    root = Path("/workspace")
    report = {"rc_python": platform.python_version(), "checks": []}
    controller = RobotController(root / "configs/rc.mock.yaml")
    router = TerminalCommandRouter(controller)
    router.panel_handler = lambda name, desired: controller.policy_monitors.set_enabled(
        name.removeprefix("monitor-"), desired)

    def command(line, expected='succeeded'):
        identity = router.submit(line)
        result = wait(lambda: (value if value["status"] in ("succeeded", "failed", "cancelled") else None)
                      if (value := controller.get_operation(identity)) else None)
        assert result["status"] == expected, result
        return result.get("result")

    def containers():
        return controller.runtime._ids({"io.robot-control.deployment": controller.runtime.context["deployment_id"]},
                                       time.monotonic() + 10)

    def member_ids():
        return {name: dict(container=w.endpoint.container_id, host=w.host.instance_id,
                           process=w.runtime_id, pid=w.pid, version=w.config_version)
                for name, w in controller._worker.member_workers().items()}

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

        command("switch example_chain")
        current = containers()
        assert len(current) == 2 and not set(first) & set(current)
        assert controller.status()["state"] == "paused"
        consumed_before = controller.status()["consumed"]
        command("resume")
        wait(lambda: controller.status()["consumed"] > consumed_before)
        for instance in ("example-0", "example-1"):
            snapshot = command(f"policy {instance} example")
            assert snapshot["calls"] > 0 and snapshot["instance"] == instance
            assert command(f"{instance}-example")["image_count"] == 3
        assert all(abs(value - .125) < 1e-6 for value in controller.robot.read_state()["qpos"])
        hot_before = member_ids()
        hot_contexts = {i: command(f'{i}-example')['context_id'] for i in hot_before}
        with patch.object(controller.runtime, 'runner', side_effect=AssertionError('hot command called Docker')):
            command('policy example-1 set-message "registered hot command"')
        hot_after = member_ids()
        assert hot_after['example-0'] == hot_before['example-0']
        assert hot_after['example-1'] == {**hot_before['example-1'],
            'version': hot_before['example-1']['version'] + 1}
        for instance in hot_before:
            snapshot = command(f'{instance}-example')
            assert snapshot['context_id'] == hot_contexts[instance]
            assert snapshot['message'] == ('registered hot command' if instance == 'example-1' else 'example')
        assert controller.status()['state'] == 'running'
        report['hot_command'] = {'before': hot_before, 'after': hot_after, 'contexts': hot_contexts}
        report["checks"].append("two occurrences of example use distinct containers and keep upstream targets")

        command("pause")
        version = command("example-0-shared-read")["version"]
        command(f"example-0-shared-write docker {version}")
        assert command("example-1-shared-read")["value"] == "docker"
        old = member_ids()
        previous = old
        old_contexts = {i: command(f"{i}-example") for i in old}
        group_version = controller._worker.config_version
        for step, line in enumerate(('example-1-reinitialize', 'example-1-reinitialize-chunk-size 2'), 1):
            with patch.object(controller.runtime, 'runner', side_effect=AssertionError('unexpected Docker call')):
                command(line)
            live = member_ids()
            contexts = {i: command(f"{i}-example") for i in old}
            assert live['example-0'] == old['example-0']
            assert contexts['example-0']['context_id'] == old_contexts['example-0']['context_id']
            assert contexts['example-1']['context_id'] != old_contexts['example-1']['context_id']
            assert contexts['example-1']['module_id'] == old_contexts['example-1']['module_id']
            assert live['example-1'] == {**old['example-1'], 'version': old['example-1']['version'] + step}
            assert controller._worker.config_version == group_version + step
            assert controller.status()['state'] == 'paused' and controller.status()['queue_remaining'] == 0
            assert set(containers()) == {m['container'] for m in old.values()}
            report.setdefault('same_process', []).append(dict(
                before=previous, after=live,
                contexts={i: {k: [old_contexts[i][k], contexts[i][k]] for k in ('module_id', 'context_id')} for i in old}))
            previous, old_contexts = live, contexts
        # The real worker initializes, but RC receives an error in place of its final ACK.
        # Keep the old committed version and every container until a manual cold retry.
        target = controller._worker.member_workers()['example-1']
        receive = target._receive
        def lose_initialize_ack(packet):
            event = packet['event']
            if event['type'] == 'ack' and 'monitor' in event and 'observe' in event:
                packet = {**packet, 'event': {**event, 'type':'error', 'error':'injected initialization ACK failure'}}
            receive(packet)
        with patch.object(target, '_receive', side_effect=lose_initialize_ack):
            command('example-1-reinitialize-chunk-size 4', 'failed')
        assert member_ids() == previous
        assert set(containers()) == {m['container'] for m in previous.values()}
        assert controller.status()['retry_reload_command'] == 'retry-reload example-1'
        command('resume', 'failed')
        command('retry-reload example-1')
        before = member_ids()
        assert before['example-0'] == previous['example-0']
        assert all(before['example-1'][k] != previous['example-1'][k] for k in ('container','host','process'))
        assert command('example-1-example')['chunk_size'] == 4
        assert before['example-1']['version'] == previous['example-1']['version'] + 1
        report['retry_after_reinitialize'] = dict(parked=previous, recovered=before)
        command("example-1-set-chunk-size 2")
        local = member_ids()
        assert local['example-0'] == before['example-0']
        assert all(local['example-1'][k] != before['example-1'][k] for k in ('container','host','process'))
        assert local['example-1']['version'] == before['example-1']['version'] + 1
        assert set(containers()) == {m['container'] for m in local.values()}
        assert controller.status()["state"] == "paused" and controller.status()['queue_remaining'] == 0
        command("example-1-set-chunk-size-all 3")
        whole = member_ids()
        assert all(whole[name][k] != local[name][k] for name in whole for k in ('container','host','process'))
        assert whole['example-0']['version'] == local['example-0']['version']
        assert whole['example-1']['version'] == local['example-1']['version'] + 1
        assert set(containers()) == {m['container'] for m in whole.values()}
        assert controller.status()["state"] == "paused" and controller.status()['queue_remaining'] == 0
        # Lose the create result, then fail readiness on a second real attempt.
        # Both cleanup paths must leave the other instance in its container.
        original_start = controller.runtime.start
        original_wait = controller.runtime.wait_ready
        lost_attempts = []
        def lose_create_result(*args):
            lost_attempts.append(original_start(*args))
            raise TimeoutError('injected loss of create result')
        def fail_candidate(handle, cancel, deadline):
            original_wait(handle, cancel, deadline)
            raise RuntimeError('injected candidate readiness failure')
        controller.runtime.start = lose_create_result
        try:
            command('example-1-set-chunk-size 3', 'failed')
            lost, = lost_attempts
            assert lost.phase == 'removed'
            # An immediate stop may reach the new container before its host
            # installs signal handlers. RC must retain that exact forced exit.
            assert controller.exit_code == int(lost.cleanup_forced)
            assert containers() == [whole['example-0']['container']]
            controller.runtime.start = original_start
            controller.runtime.wait_ready = fail_candidate
            command('retry-reload example-1', 'failed')
            assert controller.status()['retry_reload_instance'] == 'example-1'
            assert containers() == [whole['example-0']['container']]
        finally:
            controller.runtime.start = original_start
            controller.runtime.wait_ready = original_wait
        command('retry-reload example-1')
        recovered = member_ids()
        assert recovered['example-0'] == whole['example-0']
        assert recovered['example-1']['version'] == whole['example-1']['version'] + 1
        assert set(containers()) == {m['container'] for m in recovered.values()}
        report['reload_identities'] = dict(before=before, reload_policy=local, reload_all_policy=whole, retry=recovered)
        assert command("example-0-example")["chunk_size"] == 1
        assert command("example-1-example")["chunk_size"] == 3
        command("save-config example-1")
        saved = read_config(root / "policy/example/policy.yaml")
        assert saved["id"] == "example" and saved["params"]["chunk_size"] == 3
        command("take")
        episode = controller.submit_policy_wait()
        wait(lambda: controller.status()["state"] == "running")
        command("example-1-next-result completed docker")
        receipt = controller.wait_operation(episode, 10)
        assert receipt["status"] == "succeeded" and receipt["result"] == {
            "outcome": "completed", "message": "docker", "source": {"policy_id": "example-1", "cell": 2}}
        status = controller.status()
        assert status["state"] == "paused" and status["control_owner"] == "mcp" and status["queue_remaining"] == 0
        report["checks"].append("instance globals, member and group reload identities, source save, cell 2 finish and MCP handback")
        mcp_management_checks(controller, report)
    except BaseException:
        print(json.dumps(controller.read_events(), ensure_ascii=False, default=str), flush=True)
        raise
    finally:
        router.close()
        controller.submit("exit")
        assert controller.done.wait(30), controller.status()
        (root / "outputs/example-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    forced_projects = {h.project for h in controller.runtime.handles.values() if h.cleanup_forced}
    assert forced_projects == ({lost.project} if lost.cleanup_forced else set())
    assert controller.exit_code == int(lost.cleanup_forced) and controller.robot.closed
    assert not containers() and controller._pool._bytes == 0
    report['cleanup_exit'] = dict(lost_create_forced=lost.cleanup_forced, controller_exit_code=controller.exit_code)
    report["checks"].append("all policy containers removed, image pool fully released, forced exit status preserved")
    (root / "outputs/example-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)


def on_host(evidence):
    from source_helpers import anchor_override
    # All generated configuration and deployment identity belong to this temp project.
    from project_templates import copy_templates
    sys.path.insert(0, str(ROOT / "scripts"))
    from setup_project import setup

    evidence.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="rc-example-smoke-"))
    for name in ("src", "packages/policy-sdk", "docker"):
        shutil.copytree(ROOT / name, directory / name, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    (directory / "configs").mkdir()
    shutil.copyfile(ROOT / ".env.example", directory / ".env.example")
    example = copy_templates(ROOT / "policy/example", directory / "policy/example")
    if os.environ.get("RC_TEST_WORKER314_IMAGE"):
        # setup only needs a prepared image in this isolated deployment.
        (example / "compose.yaml").write_text(json.dumps({"services": {"policy": {
            "image": os.environ["RC_TEST_WORKER314_IMAGE"]}}}))
    copy_templates(ROOT / "policy/example_chain", directory / "policy/example_chain")
    config = read_config(ROOT / "configs/rc.mock.yaml.example")
    config.update(startup_policy=None, frequency_hz=30)
    config["robot"]["initial_qpos"] = [.125] * 14
    config["timeouts"] = dict(initialize_s=30, inference_s=5, release_s=3, shutdown_s=25)
    (directory / "configs/rc.mock.yaml").write_text(json.dumps(config))
    assert setup(directory, ["example_chain"], config_only=True) == ["example"]
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
        assert len(report["checks"]) == 8, report
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
    shutil.rmtree(directory)
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
