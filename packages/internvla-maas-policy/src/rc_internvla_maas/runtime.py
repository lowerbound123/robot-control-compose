"""Qwen single processing over the SDK scheduler and a cancellable MaaS backend."""
import copy
from pathlib import Path
from time import monotonic

import numpy as np
from rc_policy_sdk.async_chunks import AsyncChunkPolicy

from .configuration import load_model_config, resolve_config_path
from .metrics import StageTimings


class AsyncMaasPolicy(AsyncChunkPolicy):
    def __init__(self, config, *, backend=None, project_root=None):
        self.config = copy.deepcopy(config)
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self._timings = StageTimings(
            ('snapshot_submit', 'predict', 'result_ready_wait', 'collect', 'postprocess', 'callback'))
        if backend is None:
            from .backend import MaasBackend
            backend = MaasBackend(self.config)
        super().__init__(backend, instruction=self.config['runtime']['instruction'],
                         chunk_size=50, request_when_remaining=30,
                         inference_timeout_s=self.config['execution']['inference_timeout_s'])

    def _launch(self, state, images, runtime):
        with self._timings.measure('snapshot_submit'):
            return super()._launch(state, images, runtime)

    def _predict(self, qpos, images, instruction):
        with self._timings.measure('predict'):
            return super()._predict(qpos, images, instruction)

    def _collect(self, runtime):
        future, started = self.pending, monotonic()
        try:
            with self._timings.measure('collect'):
                return super()._collect(runtime)
        finally:
            if future.done() and not future.cancelled() and future.exception() is None:
                _, completed = future.result()
                self._timings.record('result_ready_wait', max(0., started - completed) * 1000.)

    def _process(self, actions):
        with self._timings.measure('postprocess'):
            result = actions.copy()
            if self.config['actions']['gripper_compensation']:
                for index in (6, 13):
                    column = result[:, index]
                    column[column > -2.0] += np.float32(.15)
            return result

    def monitor(self):
        return {**self.backend.monitor(), **self._timings.snapshot()}

    def step(self, state, action_chunk, images, *, runtime):
        with self._timings.measure('callback'):
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
