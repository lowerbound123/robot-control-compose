"""Qwen model configuration and postprocessing over the shared chunk scheduler."""
import copy
from pathlib import Path

import numpy as np
from rc_policy_sdk.async_chunks import AsyncChunkPolicy

from .configuration import load_model_config, resolve_config_path


class AsyncQwenPolicy(AsyncChunkPolicy):
    def __init__(self, config, *, backend=None, project_root=None):
        self.config = copy.deepcopy(config)
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.workflow = config['model']['workflow']
        if backend is None:
            from .backend import Qwen35Backend
            backend = Qwen35Backend(self.config)
        super().__init__(backend, instruction=self.config['runtime']['instruction'],
                         chunk_size=50, request_when_remaining=30,
                         inference_timeout_s=self.config['execution']['inference_timeout_s'])

    @staticmethod
    def _process(actions):
        result = actions.copy()
        for index in (6, 13):
            column = result[:, index]
            column[column > -2.0] += np.float32(0.15)
        return result

    def switch_model(self, config_path):
        path = resolve_config_path(config_path, self.project_root)
        load_model_config(path, workflow=self.workflow, project_root=self.project_root)
        return {'params.model_config_path': str(path)}
