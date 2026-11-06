from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def test_setup_initializes_connector_device_config_without_overwriting(tmp_path):
    from config_files import configuration_updates
    folder = tmp_path / 'connector/mock/devices'
    folder.mkdir(parents=True)
    (folder / 'default.yaml.example').write_text('image_size: [64, 96]\n')
    changes, _ = configuration_updates(tmp_path)
    assert any(path == folder / 'default.yaml' for path, _ in changes)
    (folder / 'default.yaml').write_text('image_size: [100, 200]\n')
    changes, _ = configuration_updates(tmp_path)
    assert not any(path == folder / 'default.yaml' for path, _ in changes)


def test_only_acone_image_contains_ros():
    rc = (ROOT / 'docker/rc.Dockerfile').read_text()
    mock = (ROOT / 'connector/mock/Dockerfile').read_text()
    acone = (ROOT / 'connector/acone/Dockerfile').read_text()
    assert 'FROM ros:' not in rc and 'ros-humble-' not in rc
    assert 'FROM ros:' not in mock
    assert 'FROM ros:humble-' in acone
    assert '/opt/ros/' not in (ROOT / 'docker/rc-entrypoint.sh').read_text()


def test_acone_overlay_keeps_mcp_reachable_on_the_base_bridge():
    from test_setup_dynamic import read_compose
    model = read_compose(ROOT / 'compose.yaml', ROOT / 'compose.acone.yaml')
    service = model['services']['robot-control']
    assert service.get('network_mode') != 'host'
    assert service['command'][-2:] == ['--mcp', '0.0.0.0:4000']
