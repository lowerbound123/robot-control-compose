"""Episode statistics must observe real requests without changing execution."""
import importlib.util
import threading

import pytest

from llm_failure_helpers import ROOT, Provider, Runtime, done, images, make, wait


def launch(instance, runtime, count=60):
    chunk = [[0.] * 14]
    instance.policy(chunk, images(), runtime)
    runtime.consumed_actions = count
    instance.policy(chunk, images(), runtime)
    return chunk


def test_monitor_entry_has_exact_six_fields_and_does_not_collect_or_do_io():
    spec = importlib.util.spec_from_file_location('failure_entry', ROOT / 'policy/llm_failure/main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    instance = make()
    try:
        assert module.monitor(instance) == {
            'in_flight': 0, 'avg_http_s': '—', 'max_http_s': '—',
            'avg_total_tokens': '—', 'max_total_tokens': '—', 'failed_requests': 0}
    finally:
        instance.close()


def test_supplier_errors_recover_at_actual_submission_threshold_without_latching():
    from rc_llm_failure.errors import ProviderError
    provider, runtime = Provider(ProviderError('HTTP 503'), ProviderError('invalid response'), True), Runtime()
    instance = make(provider=provider)
    try:
        chunk = launch(instance, runtime)
        for step in (60, 120):
            done(instance)
            assert instance.policy(chunk, {}, runtime) is chunk
            assert runtime.finishes == []
            runtime.consumed_actions = step + 59
            instance.policy(chunk, {}, runtime)
            assert len(provider.calls) == step // 60
            runtime.consumed_actions = step + 60
            instance.policy(chunk, images(), runtime)
        done(instance)
        instance.policy(chunk, {}, runtime)
        assert len(runtime.finishes) == 1
        assert instance.monitor()['failed_requests'] == 2
        assert instance.monitor()['avg_http_s'] == instance.monitor()['avg_total_tokens'] == '—'
    finally:
        instance.close()


def test_timeout_stays_in_flight_counts_once_and_never_accepts_late_true(monkeypatch):
    import rc_llm_failure.runtime as module
    clock, gate = [100.], threading.Event()
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    provider, runtime = Provider((gate, True), False), Runtime()
    instance = make(provider=provider)
    try:
        chunk = launch(instance, runtime)
        wait(lambda: provider.calls)
        assert instance.monitor()['in_flight'] == 1
        clock[0] = 104.
        runtime.consumed_actions = 180
        for _ in range(3):
            assert instance.policy(chunk, {}, runtime) is chunk
        assert instance.monitor()['failed_requests'] == 1
        assert instance.monitor()['in_flight'] == len(provider.calls) == 1
        gate.set()
        done(instance)
        assert instance.monitor()['in_flight'] == 0
        assert instance.monitor()['failed_requests'] == 1
        instance.policy(chunk, images(90), runtime)
        done(instance)
        assert len(provider.calls) == 2
        assert [item.step for item in provider.calls[1]] == [0, 60, 180]
        assert runtime.finishes == []
    finally:
        gate.set()
        instance.close()


def test_reset_clears_episode_before_drain_and_old_completion_cannot_add_metrics():
    from rc_llm_failure.errors import ProviderError
    gate, runtime = threading.Event(), Runtime()
    instance = make(provider=Provider(ProviderError('503'), (gate, ProviderError('old'))))
    resetter = None
    try:
        chunk = launch(instance, runtime)
        done(instance)
        instance.policy(chunk, {}, runtime)
        runtime.consumed_actions = 120
        instance.policy(chunk, images(), runtime)
        wait(lambda: len(instance.provider.calls) == 2)
        assert instance.monitor()['failed_requests'] == 1
        resetter = threading.Thread(target=instance.reset_context)
        resetter.start()
        wait(lambda: instance.monitor()['failed_requests'] == 0)
        assert instance.monitor()['in_flight'] == 1
        gate.set()
        resetter.join(2)
        assert not resetter.is_alive()
        assert instance.monitor() == {'in_flight': 0, 'avg_http_s': '—', 'max_http_s': '—',
                                      'avg_total_tokens': '—', 'max_total_tokens': '—', 'failed_requests': 0}
    finally:
        gate.set()
        if resetter:
            resetter.join(2)
        instance.close()


def test_coding_errors_are_faults_and_do_not_increment_supplier_failures():
    instance, runtime = make(provider=Provider(ValueError('implementation bug'))), Runtime()
    try:
        chunk = launch(instance, runtime)
        done(instance)
        with pytest.raises(RuntimeError, match='检查异常'):
            instance.policy(chunk, {}, runtime)
        assert instance.monitor()['failed_requests'] == 0
        with pytest.raises(RuntimeError):
            instance.policy(chunk, {}, runtime)
    finally:
        instance.close()


def test_late_local_coding_failure_stays_fault_without_supplier_failure_sample(monkeypatch):
    import rc_llm_failure.runtime as module
    clock, gate = [100.], threading.Event()
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    provider, runtime = Provider((gate, ValueError('local bug'))), Runtime()
    instance = make(provider=provider)
    try:
        chunk = launch(instance, runtime)
        wait(lambda: provider.calls)
        clock[0] = 104.
        gate.set()
        done(instance)
        with pytest.raises(RuntimeError):
            instance.policy(chunk, {}, runtime)
        assert instance.monitor()['failed_requests'] == 0
    finally:
        gate.set()
        instance.close()


def test_on_time_true_verdict_is_not_timed_out_by_blocked_completion_output(monkeypatch, capsys):
    import rc_llm_failure.runtime as module
    clock, output_entered, output_release = [100.], threading.Event(), threading.Event()
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    instance, runtime = make(provider=Provider(True)), Runtime()
    original = instance._diagnostics.emit
    def emit(event):
        if event['event'] == 'llm_failure.request.completed':
            output_entered.set()
            assert output_release.wait(10), 'test completion output was not released'
        original(event)
    monkeypatch.setattr(instance._diagnostics, 'emit', emit)
    try:
        chunk = launch(instance, runtime)
        assert output_entered.wait(2)
        assert not instance.pending.done()
        assert instance._request['diagnostics']['finished_mono_s'] == 100.
        clock[0] = 105.
        for _ in range(3):
            assert instance.policy(chunk, {}, runtime) is chunk
            assert instance.monitor()['failed_requests'] == 0
            assert instance.monitor()['in_flight'] == 1
        output_release.set()
        done(instance)
        instance.policy(chunk, {}, runtime)
        assert len(runtime.finishes) == 1
        assert instance.monitor()['in_flight'] == instance.monitor()['failed_requests'] == 0
        assert 'llm_failure.collection.timeout' not in capsys.readouterr().out
    finally:
        output_release.set()
        instance.close()


@pytest.mark.parametrize('local_kind', ['coding', 'validated'])
def test_provisional_timeout_is_retracted_when_completion_is_local_fault(monkeypatch, local_kind):
    import rc_llm_failure.runtime as module
    from rc_llm_failure.errors import LocalPolicyError
    clock, gate = [100.], threading.Event()
    output_entered, output_release = threading.Event(), threading.Event()
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    error = ValueError('local bug') if local_kind == 'coding' else LocalPolicyError('local request too large')
    provider, runtime = Provider((gate, error)), Runtime()
    instance = make(provider=provider)
    original = instance._diagnostics.emit
    def emit(event):
        if event['event'] == 'llm_failure.request.error':
            output_entered.set()
            assert output_release.wait(10), 'test error output was not released'
        original(event)
    monkeypatch.setattr(instance._diagnostics, 'emit', emit)
    try:
        chunk = launch(instance, runtime)
        wait(lambda: provider.calls)
        clock[0] = 104.
        for _ in range(3):
            assert instance.policy(chunk, {}, runtime) is chunk
        assert instance.monitor()['failed_requests'] == 1
        gate.set()
        assert output_entered.wait(2)
        assert instance.monitor()['in_flight'] == 1
        assert instance.monitor()['failed_requests'] == 0
        # A completed local fault cannot be counted again while its output waits.
        instance.policy(chunk, {}, runtime)
        assert instance.monitor()['failed_requests'] == 0
        output_release.set()
        done(instance)
        with pytest.raises(RuntimeError):
            instance.policy(chunk, {}, runtime)
        assert instance.monitor()['in_flight'] == instance.monitor()['failed_requests'] == 0
        assert runtime.finishes == []
    finally:
        gate.set()
        output_release.set()
        instance.close()


def test_actual_on_time_completion_overrides_timeout_before_publication(monkeypatch):
    import rc_llm_failure.runtime as module
    clock, completion_captured, publish = [100.], threading.Event(), threading.Event()
    runtime, worker_clock_calls = Runtime(), [0]
    def now():
        if threading.get_ident() != runtime.thread:
            worker_clock_calls[0] += 1
            if worker_clock_calls[0] == 2:
                # The worker captured its completion clock before being delayed
                # while the control thread observes the unpublished outcome.
                finished = clock[0]
                completion_captured.set()
                assert publish.wait(10), 'test completion publication was not released'
                return finished
        return clock[0]
    monkeypatch.setattr(module, 'monotonic', now)
    instance = make(provider=Provider(True))
    try:
        chunk = launch(instance, runtime)
        assert completion_captured.wait(2)
        clock[0] = 105.
        instance.policy(chunk, {}, runtime)
        assert instance.monitor()['failed_requests'] == 1
        publish.set()
        done(instance)
        instance.policy(chunk, {}, runtime)
        assert len(runtime.finishes) == 1
        assert instance.monitor()['failed_requests'] == 0
    finally:
        publish.set()
        instance.close()
