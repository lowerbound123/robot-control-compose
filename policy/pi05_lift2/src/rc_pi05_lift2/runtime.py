"""Independent LIFT2 Pi05 lifecycle and configurable output gripper rules."""
import copy
from pathlib import Path

import numpy as np
from rc_policy_sdk.async_chunks import AsyncChunkPolicy

from .configuration import GRIPPER_DEFAULTS, load_model_config, resolve_config_path


class AsyncPi05Policy(AsyncChunkPolicy):
    def __init__(self, config, *, policy_config=None, backend=None, project_root=None):
        self.config = copy.deepcopy(config)
        self.policy_config = policy_config if policy_config is not None else {}
        self._config_version = None
        self.project_root = Path(project_root or Path.cwd()).resolve()
        if backend is None:
            from .backend import Pi05Backend
            backend = Pi05Backend(self.config)
        execution = self.config['execution']
        super().__init__(backend, instruction=self.config['runtime']['instruction'],
                         chunk_size=execution['chunk_size'],
                         request_when_remaining=execution['request_when_remaining'],
                         inference_timeout_s=execution['inference_timeout_s'])

    def _process(self, actions):
        params = self.policy_config.get('params', {})
        settings = {name: params.get(name, default) for name, default in GRIPPER_DEFAULTS.items()}
        result = actions.copy()
        grippers = result[:, [6, 13]]
        lower_threshold = settings['gripper_lower_threshold']
        lower = grippers if lower_threshold is None else np.where(grippers <= lower_threshold, settings['gripper_lower_value'], grippers)
        result[:, [6, 13]] = np.where(grippers >= settings['gripper_upper_threshold'], settings['gripper_upper_value'], lower)
        return result

    def step(self, state, action_chunk, images, *, runtime):
        if self._closed:
            raise RuntimeError('policy is closed')
        if self._failure is not None:
            raise self._failure
        version = getattr(runtime, 'config_version', None)
        if self._epoch is not None and runtime.control_epoch != self._epoch:
            if (runtime.control_epoch < self._epoch or version is None or self._config_version is None
                    or version <= self._config_version):
                raise RuntimeError('control epoch changed without reset or a newer configuration')
            if len(action_chunk):
                raise RuntimeError('new configuration epoch must discard the old action chunk')
            # A normal hot commit invalidates old predictions, not loaded
            # weights. Drain owned work before resetting processors/cached plan.
            super().reset()
        self._config_version = version
        return super().step(state, action_chunk, images, runtime=runtime)

    def reset(self):
        super().reset()
        self._config_version = None

    def switch_model(self, config_path):
        path = resolve_config_path(config_path, self.project_root)
        load_model_config(path, project_root=self.project_root)
        return {'params.model_config_path': str(path)}
