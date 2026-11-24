"""Action publication, refresh cadence and instance-owned language state."""
import copy
import json
from pathlib import Path
import threading

import numpy as np
import pytest

from history_helpers import model_file
from rc_policy_sdk.configuration import apply_candidate
from test_qwen35_history_runtime import Backend, config, step

SOURCE = Path(__file__).resolve().parents[1] / 'policy/a2_optimized'


def entrypoint():
    # The real worker imports main as a member of a per-instance package.
    from rc_policy_sdk.worker import _load_policy
    return _load_policy(SOURCE, "_a2_test")


@pytest.fixture
def configured_a2(tmp_path, monkeypatch):
    from robot_control.yaml_config import load_yaml_config
    module = entrypoint()
    backend = Backend()
    manifest = load_yaml_config(SOURCE / 'policy.yaml.example')
    path = model_file(tmp_path)
    monkeypatch.setattr(module, 'POLICY_ROOT', tmp_path)
    implementation = module.OptimizedPolicy
    monkeypatch.setattr(module, 'OptimizedPolicy', lambda settings, **kw: implementation(settings, backend=backend, **kw))
    context = module.initialize(manifest, runtime=None)
    try:
        yield module, context, manifest, backend, path
    finally:
        context.close()


def test_pipeline_mode_and_enable_are_hot_and_select_distinct_fusions(configured_a2):
    module, context, manifest, backend, _ = configured_a2
    old = backend.output[:4].copy()
    old[:, 0] = [0, 3, 4, 7]
    backend.output[:, 0] = np.arange(50) + 8
    backend.output[:3, 0] = [0, 3.2, 7.1]
    for key, value in [('execution.pipelined', 'true'), ('transition.trunk_mode', 'close')]:
        candidate, effects = copy.deepcopy(manifest), []
        module.configure(context, candidate, 'model_config.' + key, value, effects.append)
        assert effects == []
        apply_candidate(manifest, candidate)
    np.testing.assert_array_equal(step(context, old, 0), backend.output)
    assert context.monitor()['trunk_mode'] == 'close'

    candidate, effects = copy.deepcopy(manifest), []
    module.configure(context, candidate, 'model_config.transition.trunk_mode', '"tail"', effects.append)
    assert effects == []
    assert module.export_config(context, candidate)['files'][0]['data']['transition']['trunk_mode'] == 'tail'
    assert context.monitor()['trunk_mode'] == 'close'
    apply_candidate(manifest, candidate)
    result = step(context, old, 2)
    alpha = (1 - np.exp(-1.25 * .5)) / (1 - np.exp(-1.25))
    np.testing.assert_allclose(result[:4, 0], [0, 3, 4 + alpha * (3.2 - 4), 7.1])
    np.testing.assert_array_equal(result[4:], backend.output[3:])
    monitor = context.monitor()
    assert monitor['trunk_mode'] == 'tail' and monitor['reset_count'] == 0
    assert monitor['history_tick'] == 2


def test_a2_rejects_legacy_pipeline_mode_in_set_and_model_file(configured_a2):
    module, context, manifest, _, path = configured_a2
    candidate, effects = copy.deepcopy(manifest), []
    with pytest.raises(ValueError, match='close or tail'):
        module.configure(context, candidate, 'model_config.transition.trunk_mode', 'now', effects.append)
    assert candidate == manifest and effects == []
    raw = json.loads(path.read_text())
    raw['transition'] = {'trunk_mode': 'now'}
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='close or tail'):
        module.switch_model(context, 'model.json')
    with pytest.raises(ValueError, match='close or tail'):
        module.initialize(manifest, runtime=None)


def test_action_monitor_exposes_phase_names_without_changing_action_metrics(configured_a2):
    _, context, _, _, _ = configured_a2
    assert context.monitor()['action_ms'] == 0
    step(context, [], 0)
    result = context.monitor()
    assert result['action_ms'] == result['predict_ms'] > 0
    assert result['action_prefix_ms'] == result['prefix_ms'] == 1
    assert result['action_denoise_ms'] == result['denoise_ms'] == 2


class RecordingBackend(Backend):
    def __init__(self):
        super().__init__()
        self.chunks = 0
        self.language = None
        self.actions = []
        self.refreshes = []
        self.first_token = threading.Event()
        self.allow_token = threading.Event()

    def predict(self, *args):
        self.actions.append(self.language)
        self.chunks += 1
        return super().predict(*args)

    def refresh_language(self):
        chunk = self.chunks
        self.refreshes.append(chunk)
        self.first_token.set()
        assert self.allow_token.wait(3)
        yield
        self.language = chunk

    def reset_context(self):
        super().reset_context()
        self.chunks = 0
        self.language = None


def test_fourth_action_returns_before_language_and_reset_restarts_cadence():
    module = entrypoint()
    backend = RecordingBackend()
    settings = config()
    settings['inference'] = {'generate_language': ['Subtask']}
    policy = module.OptimizedPolicy(settings, backend=backend)
    try:
        for i in range(3):
            step(policy, [], i)
        step(policy, np.zeros((50, 14), np.float32), 3)
        assert backend.refreshes == []
        assert step(policy, [], 4).shape == (50, 14)
        assert backend.first_token.wait(1) and backend.language is None
        backend.allow_token.set()
        # Drain each queued decode slice without sleeping.
        for _ in range(3):
            policy._executor.submit(lambda: None).result()
        for i in range(5, 9):
            step(policy, [], i)
        for _ in range(3):
            policy._executor.submit(lambda: None).result()
        assert backend.actions == [None]*4 + [4]*4
        assert backend.refreshes == [4, 8]
        policy.reset()
        for i in range(3):
            step(policy, [], i)
        assert backend.actions[-3:] == [None]*3
        assert backend.refreshes == [4, 8]
    finally:
        backend.allow_token.set()
        policy.close()


def test_pending_action_overtakes_unfinished_decode():
    """The next action uses the old cache even while the fifth token is outstanding."""
    class Sliced(RecordingBackend):
        def refresh_language(self):
            self.first_token.set()
            assert self.allow_token.wait(3)
            yield
            # This slice must run after the fifth action, or that action would wait.
            assert len(self.actions) == 5
            self.language = 4
    backend = Sliced()
    policy = entrypoint().OptimizedPolicy({**config(), 'inference': {'generate_language': ['Subtask']}},
                                          backend=backend)
    from concurrent.futures import ThreadPoolExecutor
    submitted = threading.Event()
    launch = policy._launch
    def signal_launch(*args):
        launch(*args)
        submitted.set()
    try:
        for i in range(4):
            step(policy, [], i)
        assert backend.first_token.wait(1)
        policy._launch = signal_launch
        with ThreadPoolExecutor(1) as caller:
            fifth = caller.submit(step, policy, [], 5)
            assert submitted.wait(1)
            backend.allow_token.set()
            fifth.result(2)
        policy._executor.submit(lambda: None).result()
        assert backend.language == 4 and backend.actions == [None]*5
    finally:
        backend.allow_token.set()
        policy.close()


def test_disabled_language_and_refresh_failure():
    for fields in ([], ['Subtask']):
        class Failed(RecordingBackend):
            def refresh_language(self):
                self.refreshes.append(self.chunks)
                raise RuntimeError('cannot decode this snapshot')
                yield
        backend = Failed()
        policy = entrypoint().OptimizedPolicy({**config(), 'inference': {'generate_language': fields}}, backend=backend)
        try:
            for i in range(5):
                step(policy, [], i)
            assert backend.refreshes == ([4] if fields else [])
            assert bool(policy.monitor()['language_refresh_error']) == bool(fields)
            assert backend.actions == [None]*5
        finally:
            policy.close()


def test_source_is_discoverable_and_uses_history_runtime(tmp_path):
    import json
    import shutil
    from robot_control.policy_catalog import discover
    from robot_control.yaml_config import load_yaml_config
    dest = tmp_path / 'policy' / SOURCE.name
    shutil.copytree(SOURCE, dest)
    shutil.copyfile(dest / 'policy.yaml.example', dest / 'policy.yaml')
    row, = discover(dest.parent)
    assert row['valid'] and row['id'] == 'a2_optimized'
    service = load_yaml_config(SOURCE / 'compose.yaml')['services']['policy']
    assert service['image'] == 'robot-control-compose-qwen35-history:local'
    modules = json.loads(service['environment']['RC_SOURCE_MODULES'])
    assert modules['rc_qwen35_history'] in service['environment']['PYTHONPATH']
    assert (SOURCE / service['build']['additional_contexts']['policy-deps'] / 'requirements.lock').is_file()


def test_reset_discards_in_progress_language_and_queued_snapshot():
    class LongRefresh(RecordingBackend):
        def refresh_language(self):
            self.first_token.set()
            while not self.allow_token.is_set():
                yield
            self.language = self.chunks

    backend = LongRefresh()
    policy = entrypoint().OptimizedPolicy({**config(), 'inference': {'generate_language': ['Subtask']}}, backend=backend)
    try:
        for i in range(8):
            step(policy, [], i)
        assert backend.first_token.wait(1)
        policy._executor.submit(lambda: None).result()
        assert policy.monitor()['language_refresh_pending'] == 2
        policy.reset()
        backend.allow_token.set()
        assert policy.monitor()['language_refresh_pending'] == 0
        step(policy, [], 0)
        assert backend.actions[-1] is None
    finally:
        backend.allow_token.set()
        policy.close()
