"""Controller integration; callbacks never perform storage work on the UI loop."""
from contextlib import contextmanager
from pathlib import Path
import time
import uuid

from rc_policy_sdk.global_state import decode, validate_key
from .global_state import GlobalStateService


class GlobalStateControl:
    def _init_globals(self):
        self._global_service = None
        self._global_store_id = None

    @property
    def globals(self):
        if self._global_service is None:
            raise RuntimeError('global state unavailable')
        return self._global_service.proxy

    def _start_globals(self, cancel_event=None):
        settings = self.config['global_state']
        ipc = Path('/run/robot-control') if self.runtime and Path('/run/robot-control').is_dir() else self.outputs / 'ipc'
        identity = uuid.uuid4().hex[:24]
        if self.runtime:
            self.runtime.recover_globals(self._cleanup_deadline())
            self.runtime.record_global(identity, ipc)
        service = GlobalStateService(self.root / settings['path'], ipc, run_id=self.run_id,
                                     max_bytes=settings['max_bytes'], timeout_s=settings['timeout_s'])
        try:
            service.start(store_id=identity, cancel_event=cancel_event, deadline=self._cleanup_deadline())
        except BaseException:
            self.exit_code = 1
            if self.runtime:
                self.runtime.forget_global(identity)
            raise
        self._global_service, self._global_store_id = service, identity

    def _save_globals(self, deadline):
        if self._global_service is not None:
            self._global_service.finish(deadline)

    def _close_globals(self, deadline, *, reclaim):
        if self._global_service is not None:
            self._global_service.close(deadline, reclaim=reclaim)
            if reclaim and self.runtime:
                self.runtime.forget_global(self._global_store_id)

    @staticmethod
    def _is_global_read(words):
        return len(words) in (2, 3) and words[:2] == ['show', 'global']

    def _query_global(self, words):
        value = self.globals.snapshot()[1]
        if len(words) == 3:
            value = value[validate_key(words[2])]
        from .secret_config import public_value
        return public_value(value)

    def _dispatch_global(self, request):
        parts = request.line.split(maxsplit=2)
        if not (len(parts) >= 2 and parts[0] in ('set', 'unset') and parts[1].startswith('global.')):
            return False
        key = validate_key(parts[1][7:])
        if parts[0] == 'set' and len(parts) == 3:
            values, deleted = {key: decode(parts[2])}, ()
        elif parts[0] == 'unset' and len(parts) == 2:
            values, deleted = {}, (key,)
        else:
            raise ValueError('用法：set global.<key> <JSON> / unset global.<key>')

        @contextmanager
        def guard():
            with self._lock:
                if self._stop.is_set():
                    raise RuntimeError('controller is shutting down')
                if request.origin == 'mcp':
                    if request.epoch != self._control_epoch:
                        raise PermissionError('control ownership changed')
                    self._require_mcp()
                yield

        if self._global_service is None:
            raise RuntimeError('global state unavailable')
        future = self._global_service.submit(values, deleted, guard=guard)
        def complete(result):
            try:
                version = result.result()
                self.operations.update(request.operation_id, 'succeeded', result={'version': version})
                self._emit(f'global.{key} 已提交，版本 {version}')
            except Exception as exc:
                self.operations.update(request.operation_id, 'failed', error=exc)
                self._emit(f'共享状态操作失败：{exc}', 'error')
        future.add_done_callback(complete)
        return True
