"""Only the GPU model boundary is replaced in local host/worker tests."""
import json
from pathlib import Path
import threading
import time

import numpy as np
from rc_qwen35_history.runtime import HistoryPolicy as RealPolicy


class Model:
    chunk_size = 50
    history_slot_count = 32
    history_slot_stride = 15

    def __init__(self, root):
        self.root = Path(root)
        self.calls = 0

    def event(self, phase, **data):
        with (self.root / 'events.jsonl').open('a') as f:
            f.write(json.dumps({'phase': phase, 'thread': threading.get_ident(), **data})+'\n')

    def load(self): self.event('load')
    def warmup(self): self.event('warmup')
    def reset_context(self): self.event('reset')
    def close(self): self.event('close')
    def resize_history(self, image): return image.copy()

    def predict(self, qpos, images, instruction, contexts):
        assert qpos.flags.owndata
        assert all(value.flags.owndata and value.flags.writeable for value in images.values())
        assert all(item.image.flags.owndata and not item.image.flags.writeable for item in contexts)
        self.calls += 1
        control = json.loads((self.root / 'controls.json').read_text())
        self.event('predict_start', task=instruction)
        time.sleep(control.get('delay', 0))
        self.event('predict_end', task=instruction, indices=[f.index for f in contexts],
                   head=[int(f.image.flat[0]) for f in contexts], qpos=qpos.tolist())
        actions = np.full((50,14), control.get('value', .1), np.float32)
        actions[:, [6,13]] = -1
        return actions, {'prefix_ms': 1., 'denoise_ms': 2.}


def factory(config, *, policy_config, project_root):
    # LocalPolicyProcess copies code into its host root. Keep test observations
    # beside the explicitly configured model file, outside that temporary copy.
    root = Path(policy_config['params']['model_config_path']).parent
    return RealPolicy(config, policy_config=policy_config, backend=Model(root), project_root=project_root)
