"""Real terminal routes keep readonly business results and separately emit tables."""
import json

import pytest

from rc_policy_sdk.tables import table_document_text
from robot_control.terminal_commands import TerminalCommandRouter
from test_controller import setup_controller, until


@pytest.mark.parametrize('line', ['status', 'policy list', 'connector status', 'record status', 'connector list'])
def test_terminal_query_tables_keep_complete_result_and_control_snapshot(setup_controller, line):
    rc, robot, root = setup_controller
    rc.config['startup_policy'] = None
    (root / 'policy/first/main.py').write_text('raise RuntimeError("query imported policy")')
    rc.start()
    until(lambda: rc._connected)
    rc._connector_members = {'repeated-0': {'source': 'same', 'future': {'enabled': False}},
                             'repeated-1': {'source': 'same', 'new_error': '/完整/' + 'path/' * 40}}
    with rc._recorder._lock:
        rc._recorder._stats['future'] = {'count': 0, 'flags': [], 'map': {}, 'null': None}
    expected = rc.query(line)
    before = rc.status()
    cursor = rc.read_events()['next_seq']
    router = TerminalCommandRouter(rc)
    try:
        operation = router.submit(line)
        receipt = rc.wait_operation(operation, 5)
        assert receipt['status'] == 'succeeded', receipt
        assert receipt['result'] == expected
        until(lambda: any('table_document' in event for event in rc.read_events(cursor)['events']))
        event = next(event for event in rc.read_events(cursor)['events'] if 'table_document' in event)
        assert event['message'] == table_document_text(event['table_document'])
        assert json.loads(event['copy_json']) == expected
        if line in ('status', 'record status'):
            assert 'future.count' in event['message'] and '{}' in event['message'] and '[]' in event['message']
        if line == 'connector status':
            assert 'repeated-0' in event['message'] and 'repeated-1' in event['message'] and 'new_error' in event['message']
        if line == 'connector list':
            assert '无记录' in event['message']
        for key in ('generation', 'control_epoch', 'control_owner', 'state'):
            assert rc.status()[key] == before[key]
        assert rc._worker is None
        assert rc.direct.progress() == before['direct']
    finally:
        router.close()


def test_terminal_connector_list_failure_always_emits_error(setup_controller, monkeypatch):
    rc, _, _ = setup_controller
    rc.config['startup_policy'] = None
    rc.start()
    until(lambda: rc._connected)
    def unavailable():
        raise OSError('profile directory unavailable [literal]')
    monkeypatch.setattr(rc.connector_profiles, 'list', unavailable)
    router = TerminalCommandRouter(rc)
    cursor = rc.read_events()['next_seq']
    try:
        receipt = rc.wait_operation(router.submit('connector list'), 5)
        assert receipt['status'] == 'failed' and 'profile directory unavailable' in receipt['error']
        until(lambda: any('profile directory unavailable' in event['message']
                         for event in rc.read_events(cursor)['events']))
    finally:
        router.close()


def test_global_and_config_results_do_not_implicitly_acquire_tables(setup_controller):
    rc, _, _ = setup_controller
    rc.config['startup_policy'] = None
    rc.start()
    until(lambda: rc._connected)
    rc.globals.patch({'table_document': {'schema_version': 1, 'tables': []}, 'copy_json': 'ordinary'})
    router = TerminalCommandRouter(rc)
    try:
        for line in ('global show', 'config rc show'):
            cursor = rc.read_events()['next_seq']
            receipt = rc.wait_operation(router.submit(line), 5)
            assert receipt['status'] == 'succeeded'
            event = rc.read_events(cursor)['events'][-1]
            assert 'table_document' not in event
            assert json.loads(event['message']) == receipt['result']
    finally:
        router.close()


@pytest.mark.parametrize('line', ['status', 'record status', 'connector status'])
def test_terminal_sensitive_nested_objects_never_reach_table_or_headless_output(setup_controller, line):
    import copy
    from robot_control.cli import format_event
    rc, _, _ = setup_controller
    rc.config['startup_policy'] = None
    rc.start()
    until(lambda: rc._connected)
    sensitive = {'x-y': 123456, 'quoted.key': False, 'empty.object': {}, 'empty-list': []}
    with rc._recorder._lock:
        rc._recorder._stats['future'] = {'api_key': copy.deepcopy(sensitive), 'visible': 'kept'}
    rc._connector_members = {'same-0': {'future': {'api_key': copy.deepcopy(sensitive)}},
                             'same-1': {'source': 'same', 'visible': 'kept'}}
    business_snapshot = rc.query(line)
    original = copy.deepcopy(business_snapshot)
    cursor = rc.read_events()['next_seq']
    router = TerminalCommandRouter(rc)
    try:
        receipt = rc.wait_operation(router.submit(line), 5)
        assert receipt['status'] == 'succeeded', receipt
        until(lambda: any('table_document' in event for event in rc.read_events(cursor)['events']))
        event = next(event for event in rc.read_events(cursor)['events'] if 'table_document' in event)
        for output in (json.dumps(event['table_document']), event['message'], format_event(event), event['copy_json']):
            assert 'kept' in output and '***' in output
            for hidden in ('123456', 'x-y', 'quoted.key', 'empty.object', 'empty-list'):
                assert hidden not in output
        assert json.loads(event['copy_json']) == receipt['result']
        # Logging this query changes live pending/peak recording counters;
        # the already captured business snapshot must remain untouched.
        assert business_snapshot == original
        assert rc._connector_members['same-0']['future']['api_key'] == sensitive
        with rc._recorder._lock:
            assert rc._recorder._stats['future']['api_key'] == sensitive
    finally:
        router.close()
