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
