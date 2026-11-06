import copy
import json

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


def test_pause_after_config_publish_is_not_overwritten_by_success_result(example_rc, monkeypatch):
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
    identity = rc.submit('policy example config set params.message paused-after-publish')
    try:
        assert published.wait(5)
        command(rc, 'pause')
    finally:
        finish.set()
    reply = rc.wait_operation(identity, 5)
    assert reply['status'] in ('succeeded', 'cancelled'), reply
    assert rc.status()['state'] == 'paused'
    assert rc.query('policy example config show')['params']['message'] == 'paused-after-publish'
