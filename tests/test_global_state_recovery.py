from config_helpers import read_config
import json
import time
import uuid

import posix_ipc
import pytest

from rc_policy_sdk.global_state import SharedState
from robot_control.runtime_manager import BlockedCleanup, BlockedInfrastructure
from test_runtime_manager import manager


def test_owned_orphan_recovered_without_touching_other_store(tmp_path):
    runtime, engine = manager(tmp_path)
    ipc = runtime.state_dir.parent / 'ipc'
    owned = SharedState.create(ipc, {'old': 1})
    unrelated = SharedState.create(ipc, {'other': 2})
    runtime.record_global(owned.descriptor['store_id'], ipc)
    owned.close()
    try:
        runtime.cleanup_owned(time.monotonic() + 2)
        runtime.recover_globals(time.monotonic() + 2)
        with pytest.raises(posix_ipc.ExistentialError):
            SharedState.attach(owned.descriptor)
        assert unrelated.snapshot()[1] == {'other': 2}
        assert runtime.global_records() == []
    finally:
        owned.close(unlink=True)
        unrelated.close(unlink=True)


def test_foreign_record_and_unconfirmed_anchor_are_retained(tmp_path):
    runtime, engine = manager(tmp_path)
    identity = uuid.uuid4().hex[:24]
    runtime.record_global(identity, runtime.state_dir.parent / 'ipc')
    path = runtime.state_dir / f'global-{identity}.json'
    data = read_config(path)
    with pytest.raises(BlockedCleanup):
        runtime.finish_global_namespace_cleanup(set())
    assert path.exists()
    data['deployment_id'] = 'foreign'
    path.write_text(json.dumps(data))
    with pytest.raises(BlockedCleanup):
        runtime.recover_globals(time.monotonic() + 2)
    assert path.exists()
