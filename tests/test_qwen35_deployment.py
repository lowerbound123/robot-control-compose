"""Qwen's real Compose environment must declare the policy container user."""
import json

import pytest

from robot_control.config import PolicyRecord
from test_runtime_manager import manager
from test_setup_dynamic import ROOT, read_compose


@pytest.mark.parametrize('identity', ['qwen35_single', 'qwen35_twostage'])
def test_qwen_compose_declares_host_user_environment(tmp_path, identity):
    import time
    model = read_compose(ROOT / 'policy' / identity / 'compose.yaml')
    directory = tmp_path / 'policy' / identity
    directory.mkdir(parents=True)
    (directory / 'compose.yaml').write_text(json.dumps(model))
    runtime, _ = manager(tmp_path)
    spec = runtime.prepare_spec(PolicyRecord(directory, {'id': identity}), deadline=time.monotonic() + 10)
    environment = json.loads(spec.model_json)['services']['policy']['environment']
    # The pinned PyTorch Inductor cache path looks up the login name; the image
    # must therefore ship USER/LOGNAME instead of relying on passwd.
    assert environment['USER'] == environment['LOGNAME'] == 'robot-control'


@pytest.mark.parametrize('identity', [
    'qwen35_single', 'qwen35_twostage', 'qwen35_history', 'qwen35_history_lift2',
    'a2_optimized', 'a2_optimized_lift2',
])
def test_qwen_runtime_installs_locked_kernels_after_torch(identity):
    folder = ROOT / 'policy' / identity
    service = read_compose(folder / 'compose.yaml')['services']['policy']
    build = service['build']
    deps = folder / build['additional_contexts']['kernel-deps']
    lock = (deps / 'kernels.lock').read_text()
    assert 'fla-core==0.6.0' in lock
    assert 'causal-conv1d @ https://github.com/Dao-AILab/causal-conv1d/releases/download/' in lock
    assert 'cu12torch2.10cxx11abiTRUE-cp311-cp311-linux_x86_64.whl' in lock
    file = folder / build['context'] / build['dockerfile']
    base, runtime = file.read_text().split('FROM ${QWEN_RUNTIME_BASE} AS runtime', 1)
    late_layer = runtime.split('FROM runtime AS test', 1)[0]
    assert '/wheels/*.whl' in base and 'kernels.lock' not in base
    assert 'ARG QWEN_RUNTIME_BASE=qwen-runtime-base' in base
    assert '--require-hashes' in late_layer and '-r /opt/qwen35-kernels.lock' in late_layer
