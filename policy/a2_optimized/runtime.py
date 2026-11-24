"""Schedule language work after the action future becomes available."""
from collections import deque
import logging

from rc_qwen35_history.runtime import HistoryPolicy

REFRESH_CHUNKS = 4


class OptimizedPolicy(HistoryPolicy):
    def __init__(self, config, *, backend=None, **kwargs):
        if backend is None:
            from .backend import OptimizedBackend
            backend = OptimizedBackend(config, robot_type=self.robot_type)
        self._chunks = 0
        self._refreshes = deque()
        super().__init__(config, backend=backend, **kwargs)
        self._record(language_refresh_chunks=REFRESH_CHUNKS, language_refresh_completed=0,
                     language_refresh_pending=0, language_refresh_error='')

    def _launch(self, *args):
        self.backend.capture_refresh = bool(self.config['inference']['generate_language']
                                           and (self._chunks + 1) % REFRESH_CHUNKS == 0)
        super()._launch(*args)

    def _collect(self):
        actions = super()._collect()
        self._chunks += 1
        if self.backend.capture_refresh:
            # Detach the request now. The generator does no model work until next().
            refresh = self.backend.refresh_language()
            self._executor.submit(self._enqueue_refresh, refresh)
        return actions

    def _enqueue_refresh(self, refresh):
        self._refreshes.append(refresh)
        self._record(language_refresh_pending=len(self._refreshes))
        if len(self._refreshes) == 1:
            self._advance_refresh(refresh)

    def _advance_refresh(self, refresh):
        # All model work uses the inherited single GPU worker. A queued action can
        # run between the language prefill and every subsequent token.
        if not self._refreshes or self._refreshes[0] is not refresh:
            return
        try:
            next(refresh)
        except StopIteration:
            with self._metrics_lock:
                self._metrics['language_refresh_completed'] += 1
                self._metrics['language_refresh_error'] = ''
        except Exception as exc:
            self._record(language_refresh_error=f'{type(exc).__name__}: {exc}'[:512])
            logging.getLogger(__name__).exception('language refresh failed')
        else:
            self._executor.submit(self._advance_refresh, refresh)
            return
        self._refreshes.popleft()
        self._record(language_refresh_pending=len(self._refreshes))
        if self._refreshes:
            self._executor.submit(self._advance_refresh, self._refreshes[0])

    def _discard_refreshes(self):
        while self._refreshes:
            self._refreshes.popleft().close()
        self._record(language_refresh_pending=0)

    def _drain(self):
        super()._drain()
        self._executor.submit(self._discard_refreshes).result()

    def reset(self):
        super().reset()
        self._chunks = 0
        self._record(language_refresh_error='', language_refresh_completed=0)
