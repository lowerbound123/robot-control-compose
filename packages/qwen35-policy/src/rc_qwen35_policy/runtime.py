"""Qwen model configuration and postprocessing over the shared chunk scheduler."""
import copy
from pathlib import Path

import numpy as np
from rc_policy_sdk.async_chunks import AsyncChunkPolicy

from .configuration import load_model_config, resolve_config_path


class AsyncQwenPolicy(AsyncChunkPolicy):
    def __init__(self, config, *, policy_config=None, backend=None, project_root=None):
        self.config = copy.deepcopy(config)
        # SDK hot patches update this manifest in place after validation.
        self.policy_config = policy_config if policy_config is not None else {}
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.workflow = config['model']['workflow']
        if backend is None:
            from .backend import Qwen35Backend
            backend = Qwen35Backend(self.config)
        super().__init__(backend, instruction=self._task_instruction(),
                         chunk_size=50, request_when_remaining=30,
                         inference_timeout_s=self.config['execution']['inference_timeout_s'])

    def _task_instruction(self):
        task = self.policy_config.get('params', {}).get('task')
        return self.config['runtime']['instruction'] if task is None else task

    def step(self, state, action_chunk, images, *, runtime):
        if self._failure is not None:
            raise self._failure
        instruction = self._task_instruction()
        if instruction != self.instruction:
            # Finish and discard old-task work before resetting subtask tokens.
            # The backend/model and executor remain loaded. The returned fresh
            # chunk replaces RC's old cache at this sync callback boundary.
            self.reset()
            self.instruction = instruction
            action_chunk = []
        return super().step(state, action_chunk, images, runtime=runtime)

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

    def switch_task(self, task):
        if not isinstance(task, str) or not task.strip():
            raise ValueError('task must be a nonempty string')
        return {'params.task': task}
