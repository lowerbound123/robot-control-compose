import copy
import json
import pytest

from test_example_controller import example_rc, ROOT
from project_templates import copy_templates


def command(rc, line, *, status='succeeded'):
    reply = rc.wait_operation(rc.submit(line), 12)
    assert reply['status'] == status, json.dumps(reply, ensure_ascii=False)
    return reply.get('result')


def test_configure_ordinary_and_reset_keep_process_and_config_refs(example_rc):
    rc = example_rc
    worker = rc._worker
    config = worker.config
    before = command(rc, 'policy example example')
    command(rc, 'policy example config set params.message "原始 中文"')
    assert worker is rc._worker and worker.config is config
    assert config['params']['message'] == '原始 中文'
    command(rc, 'policy example config set params.reset_token 1')
    after = command(rc, 'policy example example')
    assert after['context_id'] == before['context_id']
    assert rc.status()['state'] == 'paused'


def test_configure_reinitialize_preserves_module_and_pid(example_rc):
    rc = example_rc
    worker = rc._worker
    before = command(rc, 'policy example example')
    pid = worker.pid
    command(rc, 'policy example config set params.chunk_size 7')
    after = command(rc, 'policy example example')
    assert rc._worker is worker and worker.pid == pid
    assert after['module_id'] == before['module_id']
    assert after['context_id'] != before['context_id']
    assert after['chunk_size'] == 7


def test_rejected_candidate_retains_committed_config_and_accepts_next_command(example_rc):
    rc = example_rc
    before = rc.query('policy example config show')
    version = rc._worker.config_version
    command(rc, 'policy example config set params.chunk_size -1', status='failed')
    assert rc.query('policy example config show') == before
    assert rc._worker.config_version == version
    command(rc, 'policy example config set params.message accepted')
    assert command(rc, 'policy example example')['message'] == 'accepted'


def test_chain_configure_changes_only_target_and_refreshes_execution(example_rc):
    rc = example_rc
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'policy switch example_chain')
    group = rc._worker
    workers = group.member_workers()
    untouched = copy.deepcopy(workers['example-1'].config)
    version = workers['example-1'].config_version
    command(rc, 'policy example-0 config set execution.mode "async"')
    assert group.execution['mode'] == 'async'
    command(rc, 'policy example-0 config set params.chunk_size 6')
    assert group.member_workers() == workers
    assert workers['example-1'].config == untouched and workers['example-1'].config_version == version
    assert command(rc, 'policy example-0 example')['chunk_size'] == 6


@pytest.mark.parametrize('setting,value', [('params.message', 'paused-after-publish'), ('execution.mode', 'async')])
def test_pause_after_config_publish_is_not_overwritten_by_success_result(example_rc, monkeypatch, setting, value):
    import threading
    from test_controller import until
    rc = example_rc
    published, finish = threading.Event(), threading.Event()
    original = rc._run_configure

    def delayed_result(job):
        result = original(job)
        published.set()
        assert finish.wait(5)
        return result

    monkeypatch.setattr(rc, '_run_configure', delayed_result)
    command(rc, 'resume')
    identity = rc.submit(f'policy example config set {setting} {value}')
    try:
        assert published.wait(5)
        command(rc, 'pause')
    finally:
        finish.set()
    reply = rc.wait_operation(identity, 5)
    assert reply['status'] in ('succeeded', 'cancelled'), reply
    assert rc.status()['state'] == 'paused'
    section, field = setting.split('.')
    assert rc.query('policy example config show')[section][field] == value


def test_example_export_is_cached_and_saved_without_changing_other_instances(example_rc):
    rc = example_rc
    path = rc.registry.directory / 'example' / 'main.py'
    path.write_text(path.read_text() + '''
def export_config(context, config):
    data = {"message": config["params"]["message"]}
    return {"policy": config, "settings": {"model_config": data},
            "files": [{"path": "model.json", "data": data}]}
''')
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'policy switch example_chain')
    before = copy.deepcopy(rc.query('policy example-1 config show'))
    command(rc, 'policy example-0 config set params.message saved-model')
    view = rc.query('policy example-0 config show')
    assert view['model_config'] == {'message': 'saved-model'}
    worker = rc._worker.member_workers()['example-0']
    version, live = worker.config_version, copy.deepcopy(worker.config)
    command(rc, 'policy example-0 config save')
    assert json.loads(path.with_name('model.json').read_text()) == {'message': 'saved-model'}
    assert worker.config == live and worker.config_version == version
    assert rc.query('policy example-1 config show') == before
    command(rc, 'policy example-0 config set params.chunk_size 7')
    assert rc.query('policy example-0 config show')['model_config'] == {'message': 'saved-model'}


def test_rc_scheduling_edits_keep_action_epoch_and_cached_targets(example_rc):
    import numpy as np
    rc = example_rc
    values = np.ones((4, 14), np.float32)
    with rc._lock:
        rc.buffer.install(values)
        generation = rc.buffer.generation
    worker, context_id = rc._worker, rc._worker.context_id
    command(rc, 'policy example config set execution.mode async')
    command(rc, 'policy example config set execution.call_every_actions 7')
    assert rc.buffer.generation == generation
    np.testing.assert_array_equal(rc.buffer.remaining(), values)
    assert rc._worker is worker and worker.context_id == context_id


@pytest.mark.parametrize('mode', ['async', 'sync'])
def test_running_mode_edit_waits_for_inflight_and_keeps_execution_context(example_rc, mode):
    from test_controller import until
    rc = example_rc
    command(rc, 'policy example config set params.inference_delay_s 0.15')
    command(rc, 'policy example config set execution.mode ' + ('sync' if mode == 'async' else 'async'))
    command(rc, 'resume')
    until(lambda: rc._inflight is not None)
    worker, context_id, generation = rc._worker, rc._worker.context_id, rc.buffer.generation
    command(rc, f'policy example config set execution.mode {mode}')
    assert rc.buffer.generation == generation
    assert rc._worker is worker and worker.context_id == context_id
    consumed = rc.buffer.consumed
    until(lambda: rc.buffer.consumed > consumed)
    assert rc.status()['state'] == 'running'


def test_chain_passthrough_keeps_marker_source_and_reports_confirmed_counts(example_rc):
    from test_controller import until
    rc = example_rc
    path = rc.registry.directory / 'example' / 'main.py'
    path.write_text(path.read_text() + '''
from rc_policy_sdk.actions import ActionChunk
import numpy as np
_unmarked_policy = policy
def policy(context, state, action_chunk, images, config, *, runtime):
    result = _unmarked_policy(context, state, action_chunk, images, config, runtime=runtime)
    if not config['id'].endswith('-0') or len(action_chunk): return result
    values = np.asarray(result, dtype=np.float32)
    markers = np.zeros(values.shape, bool)
    markers[:, 0] = True
    return ActionChunk(values, ('qpos',)*len(values), clipped=markers)
''')
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'policy switch example_chain')
    workers = rc._worker.member_workers()
    command(rc, 'resume')
    until(lambda: rc.buffer.consumed >= 3)
    owner = ('example-0', workers['example-0'].context_id)
    with rc._lock:
        assert rc.buffer.clip_stats(owner)['clipped_actions'] == rc.buffer.consumed
        assert rc.buffer.clip_stats(('example-1', workers['example-1'].context_id))['clipped_actions'] == 0
