"""Pi05 lifecycle around the same chunk scheduler used by Qwen35."""
import copy
from pathlib import Path

from rc_policy_sdk.async_chunks import AsyncChunkPolicy

from .configuration import load_model_config, resolve_config_path


class AsyncPi05Policy(AsyncChunkPolicy):
    def __init__(self, config, *, backend=None, project_root=None):
        self.config = copy.deepcopy(config)
        self.project_root = Path(project_root or Path.cwd()).resolve()
        if backend is None:
            from .backend import Pi05Backend
            backend = Pi05Backend(self.config)
        execution = self.config['execution']
        super().__init__(backend, instruction=self.config['runtime']['instruction'],
                         chunk_size=execution['chunk_size'],
                         request_when_remaining=execution['request_when_remaining'],
                         inference_timeout_s=execution['inference_timeout_s'])

    def switch_model(self, config_path):
        path = resolve_config_path(config_path, self.project_root)
        load_model_config(path, project_root=self.project_root)
        return {'params.model_config_path': str(path)}
