"""Qwen single processing over the SDK scheduler and a cancellable MaaS backend."""
import copy
from pathlib import Path

import numpy as np
from rc_policy_sdk.async_chunks import AsyncChunkPolicy

from .configuration import load_model_config, resolve_config_path


class AsyncMaasPolicy(AsyncChunkPolicy):
    def __init__(self, config, *, backend=None, project_root=None):
        self.config = copy.deepcopy(config)
        self.project_root = Path(project_root or Path.cwd()).resolve()
        if backend is None:
            from .backend import MaasBackend
            backend = MaasBackend(self.config)
        super().__init__(backend, instruction=self.config['runtime']['instruction'],
                         chunk_size=50, request_when_remaining=30,
                         inference_timeout_s=self.config['execution']['inference_timeout_s'])

    def _process(self, actions):
        result = actions.copy()
        if self.config['actions']['gripper_compensation']:
            for index in (6, 13):
                column = result[:, index]
                column[column > -2.0] += np.float32(.15)
        return result

    def monitor(self):
        return self.backend.monitor()

    def step(self, state, action_chunk, images, *, runtime):
        try:
            self.backend.check_health()
            return super().step(state, action_chunk, images, runtime=runtime)
        except Exception as error:
            self._failure = error
            raise

    def reset(self):
        if self._failure is not None:
            raise self._failure
        self.backend.check_health()
        self.backend.abort()
        super().reset()

    def close(self):
        if self._closed:
            return
        try:
            self.backend.abort()
        finally:
            super().close()

    def switch_model(self, config_path):
        path = resolve_config_path(config_path, self.project_root)
        load_model_config(path, project_root=self.project_root)
        return {'params.model_config_path': str(path)}
