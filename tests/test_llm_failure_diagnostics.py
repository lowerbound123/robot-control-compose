import json
import threading

import pytest

from llm_failure_helpers import Provider, Runtime, done, images, make, manifest, wait


def events(capsys):
    lines = capsys.readouterr().out.splitlines()
    assert all(len(line.encode('utf-8')) + 1 <= 8000 for line in lines)
    values = [json.loads(line) for line in lines]
    assert all(value['event'].startswith('llm_failure.') for value in values)
    return values


def launch(instance, runtime):
    chunk = [[0.] * 14]
    instance.policy(chunk, images(), runtime)
    runtime.consumed_actions += 60
    instance.policy(chunk, images(), runtime)
    return chunk


def test_every_real_query_has_unique_id_instance_and_independent_collection_time(monkeypatch, capsys):
    import rc_llm_failure.runtime as module
    clock = [100.]
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    config = manifest()
    config['id'] = 'llm_failure-2'
    config['params']['provider']['timeout_s'] = 30.
    provider, runtime = Provider(), Runtime(300, epoch=7)
    instance = make(config, provider)
    try:
        chunk = launch(instance, runtime)
        done(instance)
        clock[0] = 120.
        instance.policy(chunk, {}, runtime)
        first = events(capsys)
        assert [value['event'] for value in first] == [
            'llm_failure.request.started', 'llm_failure.request.completed', 'llm_failure.collection.accepted']
        assert len({value['query_id'] for value in first}) == 1
        assert all(value['instance_id'] == 'llm_failure-2' for value in first)
        assert all((value['step'], value['history_size'], value['epoch'], value['generation']) ==
                   (60, 2, 7, 0) for value in first)
        finished, collected = first[1:]
        assert finished['started_mono_s'] == finished['finished_mono_s'] == 100.
        assert finished['elapsed_s'] == 0.
        assert finished['started_wall_s'] <= finished['finished_wall_s']
        assert finished['parsed_failed'] is False
        assert collected['collection_wait_s'] == 20.
        assert collected['finished_mono_s'] == 100.
        runtime.consumed_actions = 420
        instance.policy(chunk, images(), runtime)
        done(instance)
        instance.policy(chunk, {}, runtime)
        second = events(capsys)
        assert first[0]['query_id'] != second[0]['query_id']
        assert len(provider.calls) == 2
    finally:
        instance.close()


@pytest.mark.parametrize('operation', ['reset', 'close'])
@pytest.mark.parametrize('answer', [True, RuntimeError('obsolete request')])
def test_uncollected_result_is_discarded_on_invalidation(operation, answer, capsys):
    gate, provider, runtime = threading.Event(), Provider(), Runtime()
    provider.responses.append((gate, answer))
    instance = make(provider=provider)
    try:
        launch(instance, runtime)
        wait(lambda: provider.calls)
        gate.set()
        done(instance)
        if operation == 'reset':
            instance.reset_context()
        else:
            instance.close()
        values = events(capsys)
        terminal = 'llm_failure.request.error' if isinstance(answer, Exception) else 'llm_failure.request.completed'
        assert [value['event'] for value in values] == [
            'llm_failure.request.started', terminal, 'llm_failure.collection.discarded']
        assert len({value['query_id'] for value in values}) == 1
        assert values[1]['invalidated'] is False
        assert values[2]['invalidated'] is True
        assert values[2]['reason'] == operation
        assert runtime.finishes == []
    finally:
        gate.set()
        instance.close()


def test_pending_timeout_logs_once_and_eventual_completion_is_still_recorded(monkeypatch, capsys):
    from rc_llm_failure.errors import ProviderError
    import rc_llm_failure.runtime as module
    clock, gate = [100.], threading.Event()
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    provider, runtime = Provider((gate, True)), Runtime()
    instance = make(provider=provider)
    try:
        chunk = launch(instance, runtime)
        wait(lambda: provider.calls)
        clock[0] = 104.
        for _ in range(2):
            with pytest.raises(ProviderError, match='超时'):
                instance.policy(chunk, {}, runtime)
        before = events(capsys)
        assert [value['event'] for value in before] == [
            'llm_failure.request.started', 'llm_failure.collection.timeout']
        assert before[1]['pending'] is True
        assert before[1]['collection_wait_s'] is None
        clock[0] = 105.
        gate.set()
        done(instance)
        instance.close()
        after = events(capsys)
        assert [value['event'] for value in after] == [
            'llm_failure.request.completed', 'llm_failure.collection.discarded']
        assert after[0]['late'] is True
        assert after[0]['invalidated'] is True
        assert after[1]['reason'] == 'timeout'
        assert len({value['query_id'] for value in before + after}) == 1
        assert runtime.finishes == []
    finally:
        gate.set()
        instance.close()


def test_result_that_completed_after_deadline_is_discarded(monkeypatch, capsys):
    from rc_llm_failure.errors import ProviderError
    import rc_llm_failure.runtime as module
    clock, gate = [100.], threading.Event()
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    provider, runtime = Provider((gate, True)), Runtime()
    instance = make(provider=provider)
    try:
        chunk = launch(instance, runtime)
        wait(lambda: provider.calls)
        clock[0] = 104.
        gate.set()
        done(instance)
        clock[0] = 109.
        with pytest.raises(ProviderError, match='超时'):
            instance.policy(chunk, {}, runtime)
        values = events(capsys)
        assert [value['event'] for value in values] == [
            'llm_failure.request.started', 'llm_failure.request.completed', 'llm_failure.collection.discarded']
        assert values[1]['elapsed_s'] == 4.
        assert values[2]['collection_wait_s'] == 5.
        assert values[2]['reason'] == 'deadline_exceeded'
        assert runtime.finishes == []
    finally:
        gate.set()
        instance.close()


def test_provider_error_is_recorded_before_next_policy_callback_and_redacted(capsys):
    from rc_llm_failure.errors import ProviderError
    secret = manifest()['params']['provider']['api_key']
    instance = make(provider=Provider(ProviderError('provider echoed ' + secret)))
    runtime = Runtime()
    try:
        chunk = launch(instance, runtime)
        done(instance)
        before = events(capsys)
        assert [value['event'] for value in before] == [
            'llm_failure.request.started', 'llm_failure.request.error']
        assert secret not in json.dumps(before)
        assert '[REDACTED]' in before[1]['error']
        with pytest.raises(ProviderError):
            instance.policy(chunk, {}, runtime)
        after = events(capsys)
        assert after[0]['event'] == 'llm_failure.collection.discarded'
        assert after[0]['reason'] == 'provider_error'
        assert secret not in json.dumps(after)
    finally:
        instance.close()


def test_diagnostic_output_failure_does_not_change_boolean_decision(monkeypatch):
    provider, runtime = Provider(True), Runtime()
    instance = make(provider=provider)
    def broken_output(*args, **kwargs):
        raise OSError('closed output')
    # Output failures are external failures; the actual query and finish path run.
    monkeypatch.setattr('builtins.print', broken_output)
    try:
        chunk = launch(instance, runtime)
        done(instance)
        instance.policy(chunk, {}, runtime)
        assert len(runtime.finishes) == 1
    finally:
        instance.close()
