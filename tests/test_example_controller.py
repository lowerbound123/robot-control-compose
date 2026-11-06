"""RC through its public commands with only the shipped example in the catalog."""
import asyncio
import json
from pathlib import Path

import pytest

from config_helpers import read_config
from host_helpers import LocalPolicyProcess
from project_templates import copy_templates
from robot_control.controller import RobotController
from robot_control.mcp_server import MCPService
from robot_control.terminal_commands import TerminalCommandRouter
from test_controller import until

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def example_rc(tmp_path, monkeypatch, request):
    folder = copy_templates(ROOT / "policy/example", tmp_path / "policy/example")
    mode, scheduling = getattr(request, "param", ("qpos", "sync"))
    manifest = read_config(folder / "policy.yaml")
    manifest["execution"].update(control_mode=mode, mode=scheduling, call_every_actions=3)
    manifest["params"]["chunk_size"] = 4
    (folder / "policy.yaml").write_text(json.dumps(manifest))
    config = read_config(ROOT / "configs/rc.mock.yaml.example")
    config.update(policy_directory="policy", startup_policy="example", outputs_directory="outputs",
                  reset_qpos=[0.] * 14, timeouts=dict(initialize_s=10, inference_s=5, release_s=3))
    config["robot"].update(initial_qpos=[.01] * 14, initial_epos=[.02] * 14, image_size=[12, 16])
    (tmp_path / "rc.yaml").write_text(json.dumps(config))
    monkeypatch.chdir(tmp_path)  # snapshot and every other file belong to this deployment.
    rc = RobotController(tmp_path / "rc.yaml", process_factory=LocalPolicyProcess)
    rc.start()
    try:
        until(lambda: rc.status()["policy"] == "example" and rc.status()["state"] == "paused", 10)
        yield rc
    finally:
        rc.submit("exit")
        assert rc.done.wait(15), rc.status()
        assert rc.exit_code == 0 and rc.robot.closed and rc._pool._bytes == 0, rc.read_events()


def command(rc, line, *, status="succeeded"):
    identity = rc.submit_control(line) if line in ("take", "return") else rc.submit(line)
    reply = rc.wait_operation(identity, 12)
    assert reply["status"] == status, json.dumps(reply, ensure_ascii=False)
    return reply.get("result")


@pytest.mark.parametrize("example_rc", [(mode, scheduling) for mode in ("qpos", "epos", "mixed")
                                        for scheduling in ("sync", "async")], indirect=True)
def test_execution_sends_full_targets_in_the_selected_coordinates(example_rc):
    rc = example_rc
    execution = rc.query("policy example config show")["execution"]
    mode = execution["control_mode"]
    command(rc, "resume")
    until(lambda: rc.status()["consumed"] >= 4, 6)
    command(rc, "pause")
    modes = {entry["left"] for entry in rc.robot.command_modes}
    assert all(entry["left"] == entry["right"] for entry in rc.robot.command_modes)
    assert ({"epos", "qpos"} if mode == "mixed" else {mode}) <= modes
    # Paused ticks can hold joints in qpos; XYZ must always use actual eef feedback.
    for values, entry in zip(rc.robot.commands, rc.robot.command_modes):
        assert values[0] == pytest.approx(.02 if entry["left"] == "epos" else .01)


def test_example_chain_supports_both_reload_scopes(example_rc):
    rc = example_rc
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'policy switch example_chain')
    chain = rc._worker
    first = chain.monitor_members()
    command(rc, 'policy example-1 set-chunk-size 5')
    local = rc._worker.monitor_members()
    assert rc._worker is chain and local['example-0'] is first['example-0']
    assert first['example-1'].cleanup_confirmed and first['example-1'].pid != local['example-1'].pid
    assert rc.status()['state'] == 'paused' and rc.status()['queue_remaining'] == 0
    command(rc, 'policy example-1 set-chunk-size-all 7')
    assert all(w.cleanup_confirmed for w in local.values())
    assert rc._worker is not chain and rc._worker.runnable and rc.status()['state'] == 'paused'
    assert command(rc, 'policy example-1 example')['chunk_size'] == 7
    assert command(rc, 'policy example-0 example')['chunk_size'] == 4


def test_example_reinitializes_only_target_in_loaded_module(example_rc):
    rc = example_rc
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'policy switch example_chain')
    group = rc._worker
    workers = group.member_workers()
    before = [command(rc, f'policy example-{i} example') for i in range(2)]
    versions = {key: w.config_version for key, w in workers.items()}
    version = group.config_version
    # This is the isolated deployment copy. A fresh import or a disk preflight would fail.
    for directory in (workers['example-1'].policy_dir, workers['example-1'].loaded_directory):
        (directory / 'main.py').write_text('raise RuntimeError("reimported")')
    for iteration, line in enumerate(('policy example-1 reinitialize', 'policy example-1 reinitialize-chunk-size 6'), 1):
        command(rc, line)
        assert group is rc._worker and group.member_workers() == workers
        after = [command(rc, f'policy example-{i} example') for i in range(2)]
        assert after[0]['context_id'] == before[0]['context_id']
        assert after[1]['module_id'] == before[1]['module_id']
        assert after[1]['context_id'] != before[1]['context_id']
        assert workers['example-0'].config_version == versions['example-0']
        assert workers['example-1'].config_version == versions['example-1'] + iteration
        assert group.config_version == version + iteration
        assert after[1]['chunk_size'] == (4 if iteration == 1 else 6)
        assert rc.status()['state'] == 'paused' and rc.status()['queue_remaining'] == 0
        before = after


def test_cancel_after_local_initialize_ack_revokes_candidate_and_requires_cold_retry(example_rc, monkeypatch):
    rc = example_rc
    target = rc._worker
    old_version = target.config_version
    initialize = target.initialize_context

    def pause_before_commit(*args):
        flags = initialize(*args)
        command(rc, 'pause')
        return flags

    monkeypatch.setattr(target, 'initialize_context', pause_before_commit)
    command(rc, 'policy example reinitialize-chunk-size 7', status='cancelled')
    assert rc._worker is target and target.config_version == old_version
    assert target not in rc._global_service.sessions
    with pytest.raises(RuntimeError, match='stale policy context'):
        rc._external.handle(target, dict(method='register', remote_id='stale', context_id=target.context_id))
    assert rc.status()['retry_reload_command'] == 'policy example retry'
    command(rc, 'policy example config save')
    assert read_config(rc.registry.directory / 'example/policy.yaml')['params']['chunk_size'] == 4
    command(rc, 'resume', status='failed')
    command(rc, 'policy example retry')
    assert rc._worker.pid != target.pid and rc._worker.config_version == old_version + 1
    assert command(rc, 'policy example example')['chunk_size'] == 7 and rc.status()['state'] == 'paused'


@pytest.mark.parametrize('stage', ['validate_reinitialize', 'release_context'])
def test_cancel_before_release_dispatch_retains_active_context(example_rc, monkeypatch, stage):
    rc = example_rc
    target = rc._worker
    before = command(rc, 'policy example example')['context_id']
    original = getattr(target, stage)

    def pause_before_dispatch(*args, **kwargs):
        command(rc, 'pause')
        return original(*args, **kwargs)

    monkeypatch.setattr(target, stage, pause_before_dispatch)
    command(rc, 'policy example reinitialize', status='cancelled')
    assert rc._worker is target and rc.status()['retry_reload_command'] is None
    assert command(rc, 'policy example example')['context_id'] == before
    command(rc, 'resume')


def test_snapshot_record_pose_and_config_lifecycle(example_rc):
    rc = example_rc
    router = TerminalCommandRouter(rc)
    try:
        old = rc._worker
        before = rc.status()["config_version"]
        command(rc, 'policy example config set params.message "热更新"')
        assert rc._worker is old and rc.status()["config_version"] == before + 1
        assert command(rc, "policy example example")["message"] == "热更新"
        command(rc, "config rc set frequency_hz 60")
        assert rc.query("config rc show")["draft"]["frequency_hz"] == 60
        command(rc, "record start")
        command(rc, "resume")
        until(lambda: rc.status()["consumed"] >= 3, 6)
        command(rc, "pose save example_pose")
        assert read_config(rc.outputs / "state_saves/example_pose.json")["qpos"][0] == pytest.approx(.01)
        snapshot = router.submit("snapshot").result(5)
        frames = read_config(snapshot / "state.json")["images"]
        assert set(frames) == {"head", "left_wrist", "right_wrist"}
        assert all((snapshot / frame["file"]).is_file() for frame in frames.values())
        command(rc, "reset")
        until(lambda: rc.status()["hold_name"] == "reset")
        command(rc, "pose restore example_pose")
        until(lambda: rc.status()["hold_name"] == "example_pose")
        assert rc.robot.read_state()["qpos"][0] == pytest.approx(.01)
        command(rc, "policy example set-chunk-size 6")
        assert old.cleanup_confirmed and rc._worker is not old and rc.status()["state"] == "paused"
        assert command(rc, "policy example example")["chunk_size"] == 6
        command(rc, "policy example config save")
        assert read_config(rc.registry.directory / "example/policy.yaml")["params"]["chunk_size"] == 6
        folder = Path(command(rc, "record stop")["path"])
        policy = [json.loads(line) for line in (folder / "policy.jsonl").read_text().splitlines()]
        assert any(row["event"] == "policy.result" and row["instance_id"] == "example" for row in policy)
        assert 'control.published' in (folder / "control.jsonl").read_text()
        assert (folder / "configs/initial.json").is_file()
    finally:
        router.close()


@pytest.mark.parametrize("example_rc", [("qpos", "sync"), ("qpos", "async")], indirect=True)
def test_slow_policy_trims_only_targets_consumed_during_async_inference(example_rc, monkeypatch):
    rc = example_rc
    scheduling = rc.query("policy example config show")["execution"]["mode"]
    decisions = []
    accept = rc.buffer.accept
    def observe_accept(token, chunk):
        accepted = accept(token, chunk)
        decisions.append((token.consumed, rc.buffer.last_trim, accepted))
        return accepted
    monkeypatch.setattr(rc.buffer, "accept", observe_accept)
    command(rc, "policy example config set params.inference_delay_s 0.18")
    command(rc, "resume")
    # The first 4-frame batch triggers a second request after 3 sends. The last
    # frame is still in the buffer while the real worker handles that request.
    until(lambda: len(decisions) >= 2, 6)
    assert decisions[1] == ((3, 1, False) if scheduling == "async" else (3, 0, True))


@pytest.fixture
def example_mcp(example_rc):
    service = MCPService(example_rc, {"port": 0}, allow_ephemeral=True)
    service.start()
    try:
        yield service
    finally:
        service.close()


@pytest.mark.asyncio
async def test_mcp_http_reads_images_and_direct_motion(example_rc, example_mcp):
    from test_mcp import client
    async with client(example_mcp.url) as session:
        async def call(name, **args):
            reply = await session.call_tool(name, args)
            assert not reply.is_error, reply
            return reply.structured_content

        assert (await call("status"))["control_owner"] == "policy"
        assert [row["id"] for row in (await call("policies"))["result"]] == ["example"]
        assert (await call("show", scope="policy"))["id"] == "example"
        assert "switch" in (await call("help"))["result"]
        assert "epos" in (await call("props"))["policy_modes"]
        state = await call("state", include_images=True, cameras=["head"])
        assert [im["camera"] for im in state["images"]] == ["head"]
        assert (await call("command", line="status", wait=True))["result"]["policy"] == "example"
        assert (await call("events"))["events"]
        taken = await call("take", wait=True)
        assert taken["controller"]["control_owner"] == "mcp"
        sent = await call("send_command", commands="both:close", wait=True)
        joints = await call("move_joints", targets={"left": {"joint_pos": [.035] * 6}}, wait=True)
        assert (await call("get_operation", operation_id=joints["operation_id"]))["status"] == "succeeded"
        assert example_rc.robot.read_state()["qpos"][:6] == pytest.approx([.035] * 6)
        target = [.04, .02, .02, .02, .02, .02]
        eef = await call("move_eef", targets={"right": {"end_pos": target}}, wait=True)
        assert example_rc.robot.read_state()["eef"][7:13] == pytest.approx(target)
        returned = await call("return", wait=True)
        assert returned["controller"]["control_owner"] == "policy"
        with example_rc._lock:
            messages = [row['message'] for row in example_rc.read_events(limit=1000)['events']]
        for tool, receipt in [('take', taken), ('send_command', sent), ('move_joints', joints),
                              ('move_eef', eef), ('return', returned)]:
            assert any(message.startswith(f'MCP {tool} succeeded:') and receipt['operation_id'] in message
                       for message in messages)


def test_mcp_take_direct_control_and_result_handback(example_rc, example_mcp):
    rc = example_rc
    def tool(name, **args):
        return example_mcp.submit_tool(name, args).result(8)
    move = "left:up:0.001 | right:hold"
    assert "read-only" in tool("send_command", commands=move).structured_content["error"]
    for outcome in ("completed", "terminated", "pause", "fault"):
        assert not tool("take").is_error and rc.status()["control_owner"] == "mcp"
        assert not tool("send_command", commands=move, wait=True).is_error
        episode = tool("return_and_wait", timeout_s=0).structured_content["operation_id"]
        until(lambda: rc.status()["state"] == "running")
        command(rc, f"policy example next-result {outcome} demo")
        receipt = rc.wait_operation(episode, 6)
        queried = tool("get_policy_result", operation_id=episode).structured_content
        assert queried["finished"] and queried["status"] == receipt["status"]
        until(lambda: any(row['message'].startswith(f'MCP return_and_wait {receipt["status"]}:')
                          and episode in row['message'] for row in rc.read_events(limit=1000)['events']))
        if outcome in ("completed", "terminated"):
            assert receipt["result"] == {"outcome": outcome, "message": "demo",
                                         "source": {"policy_id": "example"}}
        else:
            assert receipt["status"] == ("cancelled" if outcome == "pause" else "failed")
        assert rc.status()["control_owner"] == ("policy" if outcome == "pause" else "mcp")
    until(lambda: rc._worker is None)
    assert rc.status()["control_owner"] == "mcp"


@pytest.mark.asyncio
async def test_tui_panels_and_chain_editor_with_two_examples(example_rc):
    from robot_control.graphics import GraphicsSupport
    from robot_control.tui import RobotControlApp
    from test_policy_manager_tui import eventually
    rc = example_rc
    app = RobotControlApp(rc, start_controller=False, graphics=GraphicsSupport("kitty"))
    async with app.run_test(size=(110, 36)) as pilot:
        for line in ("camera show", "monitor", "monitor example"):
            app.router.submit(line)
        await eventually(pilot, lambda: rc.policy_monitors.snapshot().get("example", {}).get("values"))
        app.router.submit("policy manage")
        panel = app.query_one("#policy-manager")
        await eventually(pilot, lambda: panel.initialized and panel.query_one("#chain-cells").has_focus)
        # The empty editor has just one available source, the actual example.
        for _ in range(2):
            await pilot.press("d", "enter")
        assert panel.policies == ["example", "example"]
        app.router.submit("policy manage save")
        path = rc.registry.directory / "default_chain/policy.yaml"
        await eventually(pilot, lambda: not panel.disabled and panel.saved == panel.policies)
        assert read_config(path)["policies"] == ["example", "example"]
        app.router.submit("policy manage apply")
        await eventually(pilot, lambda: rc.status()["policy"] == "default_chain", timeout=12)
        assert rc.status()["state"] == "paused"
        for instance in ("example-0", "example-1"):
            assert (await asyncio.to_thread(command, rc, f"policy {instance} example"))["instance"] == instance
        rc.submit("exit")
        await eventually(pilot, lambda: not app.is_running, timeout=15)
