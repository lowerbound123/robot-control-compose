"""Shipped Pi05 callbacks through real SDK host/worker and shared RGB images."""
import json
import os
from pathlib import Path
import shutil

import numpy as np

from host_helpers import LocalPolicyProcess
from pi05_helpers import checkpoint_fixture
from rc_policy_sdk.images import ImagePool
from test_policy_monitor_worker import wait_until

ROOT = Path(__file__).resolve().parents[1]

MODEL_BOUNDARY = '''
import numpy as np
_Policy = AsyncPi05Policy
class _FixtureBackend:
    def __init__(self, config, root):
        self.chunk_size = config['execution']['chunk_size']
        self.root = root
    def load(self): pass
    def warmup(self): pass
    def reset_context(self): pass
    def predict(self, qpos, images, instruction):
        assert all(image.flags.writeable for image in images.values()), 'background retained a shared view'
        actions = np.repeat(qpos[None], self.chunk_size, axis=0)
        actions[:, 0] = float(images['head'][0, 0, 0]) / 100
        return actions
    def close(self): (self.root / 'released').write_text('released')
def AsyncPi05Policy(config, *, project_root):
    return _Policy(config, project_root=project_root, backend=_FixtureBackend(config, project_root))
'''


def test_shipped_pi05_entrypoint_releases_images_and_resets_in_real_worker(tmp_path, monkeypatch):
    directory = tmp_path / 'pi05'
    directory.mkdir()
    checkpoint_fixture(directory)
    source = ROOT / 'policy/pi05'
    shutil.copyfile(source / 'compose.yaml', directory / 'compose.yaml')
    config = json.loads((source / 'policy.json.example').read_text())
    (directory / 'policy.json').write_text(json.dumps(config))
    # Replace only the model boundary; all shipped entrypoint callbacks, config
    # validation, shared scheduler, SDK workers and image transport remain real.
    (directory / 'main.py').write_text((source / 'main.py').read_text() + MODEL_BOUNDARY)
    python_paths = [str(ROOT / 'src'), str(ROOT / 'packages/policy-sdk/src'),
                    str(ROOT / 'packages/pi05-policy/src')]
    if os.environ.get('PYTHONPATH'): python_paths.append(os.environ['PYTHONPATH'])
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(python_paths))
    worker = LocalPolicyProcess(directory, config, init_timeout_s=10, inference_timeout_s=5, release_timeout_s=3)
    pool = ImagePool(tmp_path / 'frames', cameras=('head', 'left_wrist', 'right_wrist'))
    for name in ('head', 'left_wrist', 'right_wrist'):
        pool.publish(name, np.full((2, 3, 3), 7, np.uint8), 1.)
    lease = pool.acquire()
    try:
        worker.start()
        qpos = [0.25] * 14
        qpos[6] = qpos[13] = -1
        request = {'request_id': 1, 'generation': 1, 'config_version': 1,
                   'state': {'qpos': qpos}, 'action_chunk': [], 'images': lease.descriptors,
                   'runtime': {'observation_id': 'pi05-test', 'control_epoch': 1, 'consumed_actions': 0}}
        worker.submit(request)
        response = wait_until(worker.poll, 5)
        assert response['type'] == 'result' and response['images_released'] is True
        actions = np.asarray(response['actions'])
        assert actions.shape == (50, 14)
        np.testing.assert_allclose(actions[:, 0], .07)
        np.testing.assert_equal(actions[:, [6, 13]], -1)
        worker.reset_context()
        worker.submit({**request, 'request_id': 2, 'generation': 2,
                       'runtime': {'observation_id': 'pi05-new', 'control_epoch': 2, 'consumed_actions': 7}})
        assert wait_until(worker.poll, 5)['images_released'] is True
    finally:
        forced = worker.close()
        assert worker.cleanup_confirmed, 'reader exit proof required before releasing shared images'
        lease.release()
        pool.close()
    assert not forced
