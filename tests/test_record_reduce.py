"""Record reduction commands against disposable recordings and real FFmpeg."""
import json
from pathlib import Path
import shlex
import shutil
import time

import pytest

from rc_policy_sdk.command_syntax import parse_command
from test_recording_videos import png
from test_example_controller import example_rc, command, ROOT
from project_templates import copy_templates
from test_controller import until


def recording(root, name, *, stopped=True):
    path = root / name
    (path / 'images/head').mkdir(parents=True)
    (path / 'assets/head').mkdir(parents=True)
    for index in range(3):
        (path / 'images/head' / f'{index}.png').write_bytes(png((index * 80, 50, 90)))
    (path / 'assets/head/policy-only.png').write_bytes(png((1, 2, 3)))
    (path / 'state.jsonl').write_text('{"keep":true}\n')
    events = [{'event': 'record.started'}]
    if stopped:
        events.append({'event': 'record.stopped', 'error': None})
    (path / 'session.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in events))
    return path


@pytest.mark.parametrize('target', ['record_20261113_test', 'all', 'record 中文 name'])
def test_reduce_is_a_write_command_with_one_explicit_target(target):
    value = parse_command('record reduce ' + shlex.quote(target))
    assert value.kind == 'record' and value.args == ('reduce', target)
    assert not value.readonly


@pytest.mark.parametrize('line', ['record reduce', 'record reduce all extra'])
def test_reduce_requires_exactly_one_target(line):
    with pytest.raises(ValueError):
        parse_command(line)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
@pytest.mark.parametrize('chain', [False, True])
def test_named_reduce_only_changes_selected_log_and_keeps_policy_running(example_rc, chain):
    rc = example_rc
    if chain:
        copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
        command(rc, 'policy switch example_chain')
    logs = rc.outputs / 'logs'
    selected = recording(logs, 'record_selected 中文')
    untouched = recording(logs, 'record_untouched')
    first = (selected / 'images/head/0.png').read_bytes()
    command(rc, 'resume')
    until(lambda: rc.buffer.consumed > 0)
    generation, owner, worker = rc.buffer.generation, rc.control_owner, rc._worker
    result = command(rc, 'record reduce "record_selected 中文"')
    assert result['converted'] == 1 and result['failed'] == 0
    assert (selected / 'initial.png').read_bytes() == first
    assert (selected / 'videos/head.mp4').stat().st_size > 0
    assert not (selected / 'images').exists() and not (selected / 'assets').exists()
    assert (selected / 'state.jsonl').read_text() == '{"keep":true}\n'
    assert (untouched / 'images/head/0.png').exists()
    assert rc.control_owner == owner and rc.buffer.generation == generation and rc._worker is worker
    before = rc.buffer.consumed
    until(lambda: rc.buffer.consumed > before)
    assert rc.status()['state'] == 'running'


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_reduce_all_skips_active_and_already_reduced_logs(example_rc):
    rc = example_rc
    logs = rc.outputs / 'logs'
    one = recording(logs, 'record_one')
    two = recording(logs, 'archive/record_two')
    active = command(rc, 'record start')
    until(lambda: rc.status()['recording']['samples_written'] > 0)
    result = command(rc, 'record reduce all')
    assert result['converted'] == 2 and result['skipped'] == 1
    assert (one / 'initial.png').exists() and (two / 'initial.png').exists()
    assert (Path(active['path']) / 'images').is_dir()
    assert rc.status()['recording']['session_id'] == active['session_id']
    result = command(rc, 'record reduce all')
    assert result['converted'] == 0 and result['skipped'] == 3
    command(rc, 'record stop')


@pytest.mark.parametrize('target', ['', '.', '..', '../outside', '/tmp', 'nested/path'])
def test_reducer_rejects_paths_instead_of_log_names(tmp_path, target):
    from robot_control.recording_reduce import RecordingReducer
    reducer = RecordingReducer(tmp_path)
    try:
        with pytest.raises(ValueError):
            reducer.submit(target)
    finally:
        reducer.close(time.monotonic()+2)


def test_completion_offers_all_and_only_direct_log_directories(tmp_path):
    from test_completion_service import until as completion_ready
    from robot_control.completion_service import CompletionService
    from robot_control.completion import apply_completion
    (tmp_path / 'record 中文').mkdir()
    (tmp_path / 'rc_run.jsonl').write_text('{}')
    (tmp_path / 'escape').symlink_to(tmp_path.parent, target_is_directory=True)
    service = CompletionService(None, catalog={'logs_root': str(tmp_path)}, debounce=.005)
    try:
        line = 'record reduce '
        service.request(line, len(line))
        result = completion_ready(service.poll)
        assert {item.insert_text for item in result.items} == {'all', 'record 中文'}
        item = next(item for item in result.items if item.insert_text.startswith('record'))
        completed, _ = apply_completion(line, len(line), result, item)
        assert parse_command(completed).args == ('reduce', 'record 中文')
    finally:
        service.close()


def test_missing_or_symlinked_log_fails_without_changing_external_data(tmp_path):
    from robot_control.recording_reduce import RecordingReducer
    root = tmp_path / 'logs'
    root.mkdir()
    external = recording(tmp_path, 'record_external')
    (root / 'record_link').symlink_to(external, target_is_directory=True)
    reducer = RecordingReducer(root)
    try:
        for target in ('missing', 'record_link'):
            with pytest.raises(ValueError):
                reducer.submit(target).result(3)
        assert (external / 'images/head/0.png').exists()
    finally:
        reducer.close(time.monotonic()+3)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_partial_failure_reports_counts_and_preserves_broken_session(tmp_path):
    from robot_control.recording_reduce import RecordingReducer, ReductionFailed
    broken = recording(tmp_path, 'record_broken')
    valid = recording(tmp_path, 'record_valid')
    (broken / 'images/head/0.png').write_bytes(b'broken image')
    reducer = RecordingReducer(tmp_path)
    try:
        with pytest.raises(ReductionFailed) as failure:
            reducer.submit('all').result(10)
        assert failure.value.result['converted'] == 1 and failure.value.result['failed'] == 1
        assert (broken / 'assets').is_dir() and (broken / 'images').is_dir()
        assert (valid / 'videos/head.mp4').is_file() and not (valid / 'images').exists()
    finally:
        reducer.close(time.monotonic()+3)


def test_missing_encoder_reports_failure_without_removing_images(tmp_path, monkeypatch):
    from robot_control.recording_reduce import RecordingReducer, ReductionFailed
    folder = recording(tmp_path, 'record_missing_encoder')
    monkeypatch.setenv('PATH', '')
    reducer = RecordingReducer(tmp_path)
    try:
        with pytest.raises(ReductionFailed, match='ffmpeg'):
            reducer.submit(folder.name).result(3)
        assert (folder / 'images/head/0.png').exists()
    finally:
        reducer.close(time.monotonic()+3)


def test_mcp_reduction_requires_take_but_recording_toggle_does_not(example_rc):
    rc = example_rc
    with pytest.raises(PermissionError):
        rc.submit('record reduce all', origin='mcp')
    result = rc.wait_operation(rc.submit('record start', origin='mcp'), 5)
    assert result['status'] == 'succeeded'
    rc.wait_operation(rc.submit('record stop', origin='mcp'), 5)


@pytest.mark.skipif(not shutil.which('ffprobe'), reason='ffprobe required')
def test_controller_exit_cancels_reduction_and_reaps_its_process(example_rc, tmp_path, monkeypatch):
    import os
    import sys
    rc = example_rc
    folder = recording(rc.outputs / 'logs', 'record_shutdown')
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    pidfile = tmp_path / 'encoder.pid'
    encoder = binaries / 'ffmpeg'
    encoder.write_text(f'#!{sys.executable}\nimport os,time\nopen({str(pidfile)!r}, "w").write(str(os.getpid()))\ntime.sleep(30)\n')
    encoder.chmod(0o755)
    monkeypatch.setenv('PATH', str(binaries)+os.pathsep+os.environ['PATH'])
    operation = rc.submit('record reduce record_shutdown')
    until(pidfile.exists, 4)
    rc.submit('exit')
    assert rc.done.wait(10)
    assert rc.get_operation(operation)['status'] == 'cancelled'
    assert rc.exit_code == 0
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
    assert (folder / 'images/head/0.png').exists()


@pytest.mark.skipif(not shutil.which('ffprobe'), reason='ffprobe required')
def test_busy_reducer_rejects_overlap_and_shutdown_reaps_encoder_without_deleting_images(tmp_path, monkeypatch):
    import os
    import sys
    from concurrent.futures import CancelledError
    from robot_control.recording_reduce import RecordingReducer
    root = tmp_path / 'logs'
    folder = recording(root, 'record_slow')
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    pidfile = tmp_path / 'encoder.pid'
    # Replace only the external encoder with a deliberately stalled process.
    encoder = binaries / 'ffmpeg'
    encoder.write_text(f'#!{sys.executable}\nimport os,time\nopen({str(pidfile)!r}, "w").write(str(os.getpid()))\ntime.sleep(30)\n')
    encoder.chmod(0o755)
    monkeypatch.setenv('PATH', str(binaries)+os.pathsep+os.environ['PATH'])
    reducer = RecordingReducer(root)
    try:
        future = reducer.submit('record_slow')
        until(pidfile.exists, 4)
        with pytest.raises(RuntimeError, match='正在执行'):
            reducer.submit('all')
        assert reducer.close(time.monotonic()+6) is False
        with pytest.raises(CancelledError):
            future.result()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
        assert (folder / 'images/head/0.png').exists() and (folder / 'assets').is_dir()
        assert not list(folder.glob('.recording-videos-*'))
    finally:
        reducer.close(time.monotonic()+6)
