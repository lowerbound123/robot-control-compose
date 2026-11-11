"""Connector queries must produce visible terminal completion, including failures."""
from test_connector_switch import profile
from test_example_controller import command, example_rc
from test_controller import until


def test_connector_list_prints_profile_details_and_validation_errors(example_rc):
    rc = example_rc
    profile(rc)
    folder = rc.root / rc.config.get('robot_profiles_directory', 'robots')
    (folder / 'broken.yaml').write_text('connector: missing-fields\n')
    cursor = rc.read_events()['next_seq']

    result = command(rc, 'connector list')
    assert any(row['name'] == 'second' and row['valid'] for row in result)
    events = lambda: rc.read_events(cursor)['events']
    until(lambda: any('second' in row['message'] for row in events()))
    output = '\n'.join(row['message'] for row in events())
    assert all(field in output for field in ('名称', '来源', '实例', '配置路径', '有效'))
    assert all(value in output for value in ('second', 'mock', 'body', 'second-device.json', 'true'))
    assert 'broken' in output and 'false' in output and 'requires connector' in output


def test_connector_list_prints_failed_completion(example_rc, monkeypatch):
    rc = example_rc
    # The filesystem scan is the external boundary; submission, lifecycle result
    # handling, receipts and the terminal event stream remain real.
    def unavailable():
        raise OSError('profile directory unavailable')
    monkeypatch.setattr(rc.connector_profiles, 'list', unavailable)
    cursor = rc.read_events()['next_seq']

    command(rc, 'connector list', status='failed')
    until(lambda: any(row['level'] == 'error' and 'profile directory unavailable' in row['message']
                      for row in rc.read_events(cursor)['events']))
