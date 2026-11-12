from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def test_setup_builds_explicit_lift2_connector_without_changing_defaults(tmp_path, monkeypatch):
    import compose_project
    import setup_project
    directory = tmp_path / 'connector/lift2'
    directory.mkdir(parents=True)
    (directory / 'compose.yaml').write_text('services: {}\n')
    commands = []
    monkeypatch.setattr(compose_project, 'compose_command', lambda root, args: ['base-compose', *args])
    monkeypatch.setattr(setup_project, 'run_command',
                        lambda command, *a, **k: commands.append(command) or SimpleNamespace(stdout=''))
    setup_project.prepare_images(tmp_path, [], {'uid': 1000, 'gid': 1000}, connectors=['lift2'])
    assert len(commands) == 2
    assert commands[1][-2:] == ['build', 'connector']
    assert str(directory / 'compose.yaml') in commands[1]
    commands.clear()
    setup_project.prepare_images(tmp_path, [], {'uid': 1000, 'gid': 1000})
    assert len(commands) == 1


def test_lift2_configuration_is_initialized_without_overwriting_user_network(tmp_path):
    from config_files import configuration_updates
    folder = tmp_path / 'connector/lift2/devices'
    folder.mkdir(parents=True)
    (folder/'robot-main.yaml.example').write_text('ros_ip: null\n')
    changes, _ = configuration_updates(tmp_path)
    assert any(path == folder/'robot-main.yaml' for path, _ in changes)
    (folder/'robot-main.yaml').write_text('ros_ip: 127.0.0.1\n')
    changes, _ = configuration_updates(tmp_path)
    assert not any(path == folder/'robot-main.yaml' for path, _ in changes)
