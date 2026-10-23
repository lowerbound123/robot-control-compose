"""Replace only CUDA/model work; run the production Qwen scheduler and entry."""
import json
import os
from pathlib import Path
import threading
import time

import numpy as np
from rc_qwen35_policy.runtime import AsyncQwenPolicy as RealPolicy


class Backend:
    chunk_size = 50

    def __init__(self, config):
        self.root = Path(config['model']['checkpoint']).parent
        self.workflow = config['model']['workflow']
        self.calls = 0

    def controls(self):
        return json.loads((self.root / 'controls.json').read_text())

    def record(self, phase, **details):
        row = dict(phase=phase, pid=os.getpid(), thread=threading.get_ident(),
                   workflow=self.workflow, **details)
        with (self.root / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps(row) + '\n')

    def gate(self, phase):
        self.record(phase)
        while self.controls().get('block_' + phase):
            time.sleep(.01)

    def load(self):
        self.gate('load')

    def warmup(self):
        self.record('warmup')

    def predict(self, qpos, images, instruction):
        controls = self.controls()
        self.calls += 1
        self.gate('infer')
        if controls.get('crash_infer'):
            os._exit(71)
        assert qpos.shape == (14,) and qpos.flags.owndata
        for index, name in enumerate(('head', 'left_wrist', 'right_wrist'), 1):
            frame = images[name]
            # The production scheduler must copy borrowed mmap views before
            # submitting work to its background inference thread.
            assert frame.flags.owndata and frame.flags.writeable
            assert frame.dtype == np.uint8 and frame.ndim == 3
            assert np.all(frame == index), (name, frame[0, 0].tolist())
        self.record('predicted', pixels={k: int(v.sum()) for k, v in images.items()})
        actions = np.full((50, 14), controls['value'], np.float32)
        actions[:, [6, 13]] = -3
        return actions

    def reset_context(self):
        self.record('reset')

    def close(self):
        self.gate('close')

    def describe(self):
        import sys
        return dict(pid=os.getpid(), calls=self.calls, workflow=self.workflow,
                    python=sys.version.split()[0], numpy=np.__version__)


def make_policy(config, **kwargs):
    return RealPolicy(config, backend=Backend(config), **kwargs)
