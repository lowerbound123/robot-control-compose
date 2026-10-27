"""Qwen's real Compose environment must support a host UID absent from passwd."""
import getpass
import json

import pytest

from robot_control.config import PolicyRecord
from test_runtime_manager import manager
from test_setup_dynamic import ROOT, read_compose


@pytest.mark.parametrize('identity', ['qwen35_single', 'qwen35_twostage'])
def test_qwen_username_lookup_does_not_require_a_container_passwd_entry(tmp_path, monkeypatch, identity):
    import time
    pwd = pytest.importorskip('pwd')
    model = read_compose(ROOT / 'policy' / identity / 'compose.yaml')
    directory = tmp_path / 'policy' / identity
    directory.mkdir(parents=True)
    (directory / 'compose.yaml').write_text(json.dumps(model))
    runtime, _ = manager(tmp_path)
    spec = runtime.prepare_spec(PolicyRecord(directory, {'id': identity}), deadline=time.monotonic() + 10)
    environment = json.loads(spec.model_json)['services']['policy']['environment']

    for name in ('LOGNAME', 'USER', 'LNAME', 'USERNAME'):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, str(value))

    def missing_user(uid):
        raise KeyError(f'getpwuid(): uid not found: {uid}')

    monkeypatch.setattr(pwd, 'getpwuid', missing_user)
    # This is the lookup used by the pinned PyTorch Inductor cache path. A
    # missing login environment must fail rather than use the test host user.
    assert getpass.getuser().strip()
