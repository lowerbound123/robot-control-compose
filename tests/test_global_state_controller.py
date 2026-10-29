import json
import time
import fcntl
import os

import pytest

from robot_control.global_state_file import read_state
from test_controller import setup_controller, ready, until


def command(controller, line, origin='tui', status='succeeded'):
    identity = controller.submit(line, origin=origin)
    until(lambda: controller.get_operation(identity)['status'] in ('succeeded', 'failed', 'cancelled'))
    result = controller.get_operation(identity)
    assert result['status'] == status, result
    return result


def test_global_commands_survive_switch_and_exit(setup_controller):
    controller, _, root = setup_controller
    (root / 'global.env').write_text('initial=3\n')
    ready(controller)
    assert controller.globals['initial'] == 3
    command(controller, 'set global.task {"phase":"hello world", "nested":[1,true]}')
    command(controller, 'set global.ready true')
    assert controller.query('show global task') == {'phase': 'hello world', 'nested': [1, True]}
    command(controller, 'switch second')
    assert controller.globals['ready']
    command(controller, 'unset global.initial')
    command(controller, 'unset global.missing', status='failed')
    controller.submit('exit')
    assert controller.done.wait(3)
    assert controller.exit_code == 0
    assert read_state(root / 'global.env', 1024) == {'task': {'phase': 'hello world', 'nested': [1, True]}, 'ready': True}


def test_mcp_global_reads_and_write_authority(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    command(controller, 'show global', origin='mcp')
    with pytest.raises(PermissionError):
        controller.submit('set global.item 1', origin='mcp')
    command(controller, 'take', origin='mcp')
    command(controller, 'set global.item 1', origin='mcp')
    assert controller.globals['item'] == 1


def test_invalid_global_file_blocks_robot_and_is_not_overwritten(setup_controller):
    controller, robot, root = setup_controller
    path = root / 'global.env'
    path.write_text('bad=not_json\n')
    controller.start()
    until(lambda: controller.status()['state'] == 'fault')
    assert not controller._connected
    controller.submit('exit')
    assert controller.done.wait(3)
    assert path.read_text() == 'bad=not_json\n'


def test_mcp_return_revokes_write_queued_behind_reader(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    command(controller, 'take', origin='mcp')
    fd = os.open(controller._global_service.descriptor['lock_path'], os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH)
    try:
        identity = controller.submit('set global.stale 1', origin='mcp')
        until(lambda: controller.get_operation(identity)['status'] == 'running')
        command(controller, 'return', origin='mcp')
    finally:
        os.close(fd)
    until(lambda: controller.get_operation(identity)['status'] == 'failed')
    assert 'stale' not in controller.globals


def test_failed_save_returns_nonzero_and_preserves_file(setup_controller, monkeypatch):
    controller, _, root = setup_controller
    path = root / 'global.env'
    path.write_text('old=1\n')
    ready(controller)
    memory = controller._global_service.memory
    controller.globals['new'] = 2
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr('robot_control.global_state.save_state', fail)
    try:
        controller.submit('exit')
        assert controller.done.wait(3)
        assert controller.exit_code != 0
        assert path.read_text() == 'old=1\n'
    finally:
        memory.close(unlink=True)
