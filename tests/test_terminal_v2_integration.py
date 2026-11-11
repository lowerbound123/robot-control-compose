"""Shared command grammar drives local routing and real Textual completion."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from robot_control.terminal_commands import TerminalCommandRouter


@pytest.fixture
def router():
    controller = SimpleNamespace(lines=[], logs=[], interruptions=0)
    def interrupt():
        controller.interruptions += 1
    def submit(line):
        controller.lines.append(line)
        return 'queued'
    controller.submit = submit
    controller.interrupt_tui_batches = interrupt
    controller._emit_command = lambda message, *args, **kwargs: controller.logs.append(message)
    instance = TerminalCommandRouter(controller)
    yield instance
    instance.close()


@pytest.mark.parametrize('line,expected', [
    ('camera', ('camera', None)),
    ('camera show', ('camera', True)),
    ('camera hide', ('camera', False)),
    ('camera toggle', ('camera', None)),
    ('monitor', ('monitor', None)),
    ('monitor example-1', ('monitor-example-1', None)),
])
def test_local_panel_commands_use_explicit_tree_and_toggle_monitors(router, line, expected):
    calls = []
    router.panel_handler = lambda name, desired: calls.append((name, desired))
    router.submit(line)
    assert calls == [expected]
    assert router.controller.lines == []
    assert router.controller.interruptions == 1


@pytest.mark.parametrize('action', ['', 'apply', 'save', 'close'])
def test_policy_manage_is_a_local_subtree(router, action):
    calls = []
    router.policy_manager_handler = calls.append
    router.submit('policy manage' + (' ' + action if action else ''))
    assert calls == [action or 'open']
    assert router.controller.lines == []


@pytest.mark.parametrize('line', [
    'camera on', 'monitor on', 'monitor example on', 'monitor-example',
    'manage-policy', 'example-set-message hello', 'set example.params.message hello',
    'mcp', 'help mcp', 'mcp command status', 'mcp send_command left:open',
    'mcp state []', 'mcp state null', 'mcp state {"wait":NaN}',
])
def test_removed_local_syntax_never_reaches_controller_or_panels(router, line):
    panels, managers = [], []
    router.panel_handler = lambda *args: panels.append(args)
    router.policy_manager_handler = managers.append
    with pytest.raises(ValueError):
        router.submit(line)
    assert router.controller.lines == panels == managers == []
    assert router.controller.interruptions == 1


def test_policy_raw_remainder_is_forwarded_unchanged(router):
    line = 'policy example-1 config set params.message "a \\"quote\\" and spaces"'
    assert router.submit(line) == 'queued'
    assert router.controller.lines == [line]


def test_local_help_filters_shared_grammar(router):
    router.submit('help camera')
    output = '\n'.join(router.controller.logs)
    assert all('camera ' + action in output for action in ('show', 'hide', 'toggle'))
    assert 'camera on' not in output
    assert router.controller.lines == []


@pytest.mark.asyncio
async def test_bare_camera_twice_opens_then_closes_the_panel():
    from robot_control.graphics import GraphicsSupport
    from robot_control.tui import RobotControlApp
    from robot_control.tui_panels import CameraPanel
    from test_tui import FakeController

    controller = FakeController()
    controller.robot = SimpleNamespace(camera_preview_snapshot=lambda: {})
    app = RobotControlApp(controller, graphics=GraphicsSupport('kitty'))
    async with app.run_test():
        panel = app.query_one(CameraPanel)
        assert not panel.display
        app.router.submit('camera')
        assert panel.display
        app.router.submit('camera')
        assert not panel.display
        assert controller.commands == []
        controller.done.set()


def test_mcp_help_targets_a_tool_without_calling_it(router):
    calls = []
    router.service = SimpleNamespace(tools=(
        {'name': 'state', 'description': 'read the robot', 'input_schema': {'properties': {}}},
        {'name': 'send_command', 'description': 'send motion', 'input_schema': {
            'properties': {'commands': {'type': 'string'}}, 'required': ['commands']}},
    ), submit_tool=lambda *args: calls.append(args))
    router.submit('mcp help send_command')
    output = '\n'.join(router.controller.logs)
    assert 'commands' in output and '相对运动' in output and '串行' in output
    assert 'read the robot' not in output
    assert calls == []
    with pytest.raises(ValueError):
        router.submit('mcp help missing')


def test_mcp_json_is_parsed_once_and_passed_as_an_object(router, monkeypatch):
    import robot_control.terminal_commands as terminal
    from rc_policy_sdk.command_syntax import parse_command
    parsed, calls = [], []
    def parse(line):
        parsed.append(line)
        return parse_command(line)
    monkeypatch.setattr(terminal, 'parse_command', parse)
    pending = Future()
    router.service = SimpleNamespace(tools=(), submit_tool=lambda name, args: (calls.append((name, args)), pending)[1])
    line = 'mcp command {"line":"policy example config set params.message \\"hello world\\""}'
    assert router.submit(line) is pending
    assert parsed == [line]
    assert calls == [('command', {'line': 'policy example config set params.message "hello world"'})]


@pytest.mark.asyncio
async def test_tui_completion_tracks_full_paths_instances_and_custom_commands():
    from robot_control.tui import CommandInput, RobotControlApp
    from test_tui import FakeController, wait_completion
    controller = FakeController()
    paths = ['policy hold config show', 'policy hold config set', 'policy hold config save',
             'policy hold retry', 'policy example-1 inspect']
    controller.local_commands = lambda: list(paths)
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        for prefix, expected in [
            ('policy li', 'policy list '),
            ('config rc sh', 'config rc show '),
            ('camera h', 'camera hide '),
            ('monitor example', 'monitor example-1 '),
            ('policy hold config sh', 'policy hold config show '),
            ('policy example-1 ins', 'policy example-1 inspect '),
        ]:
            field.value = prefix
            field.cursor_position = len(field.value)
            await pilot.press('tab')
            assert field.value == prefix and field.menu_open
            await wait_completion(pilot, field, [expected.strip().split()[-1]])
            await pilot.press('enter')
            assert field.value == expected
        paths[:] = ['policy hold config show', 'policy hold retry', 'policy example-1 examine']
        app.refresh_controller()
        field.value = 'policy example-1 ins'
        await pilot.press('tab')
        assert field.value == 'policy example-1 ins'
        await wait_completion(pilot, field, [])
        await pilot.press('escape')
        field.value = 'policy example-1 exa'
        field.cursor_position = len(field.value)
        await pilot.press('tab')
        await wait_completion(pilot, field, ['examine'])
        await pilot.press('enter')
        assert field.value == 'policy example-1 examine '
        raw = 'policy hold config set params.message "unaltered JSON spaces"'
        field.value = raw
        await pilot.press('tab')
        assert field.value == raw
        controller.done.set()
        await pilot.pause(.08)
