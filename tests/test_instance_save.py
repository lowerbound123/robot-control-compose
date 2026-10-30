"""Save selection stays bound to an occurrence across queued lifecycle work."""
from config_helpers import read_config
from test_chain_controller import native_controller, operation
from test_controller import setup_controller, ready


def test_instance_save_preserves_comments_and_only_changes_future_defaults(native_controller):
    controller, _, root = native_controller
    path = root / 'policy/a/policy.yaml'
    path.write_text('# source defaults\n' + path.read_text())
    operation(controller, controller.submit('a-0-change 9'))
    active = controller._worker.record
    operation(controller, controller.submit('save-config a-0'))
    assert '# source defaults' in path.read_text()
    assert read_config(path)['id'] == 'a'
    assert controller._worker.record is active
    assert [member.config['params']['value'] for member in active.members] == [9, 1, 1]
    operation(controller, controller.submit('switch default_chain'))
    assert [member.config['params']['value'] for member in controller._worker.record.members] == [9, 1, 9]


def test_failed_save_preserves_disk_defaults_and_active_instances(native_controller, monkeypatch):
    controller, _, root = native_controller
    operation(controller, controller.submit('a-0-change 9'))
    path = root / 'policy/a/policy.yaml'
    before = path.read_bytes()
    active = controller._worker.record
    def fail(*args):
        raise OSError('disk full')
    with monkeypatch.context() as patch:
        patch.setattr('robot_control.yaml_config.os.replace', fail)
        result = operation(controller, controller.submit('save-config a-0'), 'failed')
    assert 'disk full' in result['error']
    assert path.read_bytes() == before
    assert controller.registry.get('a').config['params']['value'] == 1
    assert controller._worker.record is active
    assert [member.config['params']['value'] for member in active.members] == [9, 1, 1]


def test_queued_save_never_retargets_a_new_occurrence(native_controller):
    controller, _, root = native_controller
    operation(controller, controller.submit('a-0-change 9'))
    path = root / 'policy/a/policy.yaml'
    before = path.read_bytes()
    with controller._lock:
        receipt = controller.submit('save-config a-0')
        delayed = controller._commands.get_nowait()
    operation(controller, controller.submit('switch default_chain'))
    controller._commands.put(delayed)
    controller._wake.set()
    result = operation(controller, receipt, 'cancelled')
    assert 'changed' in result['error']
    assert path.read_bytes() == before
    assert [member.config['params']['value'] for member in controller._worker.record.members] == [1, 1, 1]


def test_standalone_named_save_keeps_live_configuration_version(setup_controller):
    controller, _, root = setup_controller
    worker = ready(controller)
    version = worker.config_version
    operation(controller, controller.submit('save-config first'))
    assert controller.registry.get('first').version == worker.config_version == version
    assert read_config(root / 'policy/first/policy.yaml')['id'] == 'first'
    operation(controller, controller.submit('set policy.params.value 7'))
    assert controller.registry.get('first').version == worker.config_version == version + 1
    operation(controller, controller.submit('save-config policy first'))
    assert controller.registry.get('first').version == worker.config_version
