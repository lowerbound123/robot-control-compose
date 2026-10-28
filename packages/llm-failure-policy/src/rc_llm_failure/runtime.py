"""Single-flight model checks; only the policy callback can report a verdict."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
from time import monotonic

from rc_policy_sdk.actions import normalize_actions

from .configuration import validate_config
from .errors import ProviderError
from .images import encode_snapshot


class FailurePolicy:
    def __init__(self, config, *, provider=None):
        self.params = validate_config(config)
        self.control_mode = config['execution']['control_mode']
        if provider is None:
            from .providers import ProviderClient
            provider = ProviderClient(self.params['provider'])
        self.provider = provider
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='llm-failure')
        self._history = deque(maxlen=self.params['history_size'])
        self.pending = None
        self._request = None
        self._generation = 0
        self._epoch = self._baseline = self._last_seen = self._last_submitted = None
        self._finished = self._closed = False
        self._failure = None

    def _query(self, snapshots):
        try:
            failed = self.provider.decide(self.params['task'], self.params['prompt'], snapshots)
            if type(failed) is not bool:
                raise ProviderError('供应商响应不符合失败判断契约')
            return failed, monotonic()
        except ProviderError:
            raise
        except Exception:
            raise ProviderError('DeepSeek 检查异常') from None

    def _collect(self, runtime):
        if self.pending is None:
            return
        request = self._request
        if not self.pending.done():
            if monotonic() >= request['deadline']:
                self._failure = ProviderError('DeepSeek 检查超时')
                raise self._failure
            return
        future, self.pending = self.pending, None
        self._request = None
        if request['generation'] != self._generation or request['epoch'] != runtime.control_epoch:
            return
        metadata = {'event': 'llm_failure.check', 'step': request['step'],
                    'history_size': request['history_size'], 'elapsed_s': round(monotonic() - request['started'], 3)}
        try:
            failed, completed = future.result()
            if completed > request['deadline']:
                raise ProviderError('DeepSeek 检查超时')
        except ProviderError as exc:
            self._failure = exc
            metadata['error'] = str(exc)
            raise
        else:
            metadata['failed'] = failed
            if failed:
                runtime.finish('llm_failure：DeepSeek 判断任务已进入失败状态', outcome='terminated')
                self._finished = True
        finally:
            print(json.dumps(metadata, ensure_ascii=False, allow_nan=False), flush=True)

    def policy(self, action_chunk, images, runtime):
        if self._closed:
            raise RuntimeError('llm_failure is closed')
        # Validate without replacing or mutating the producer's return value.
        normalize_actions(action_chunk, self.control_mode)
        count, epoch = runtime.consumed_actions, runtime.control_epoch
        if type(count) is not int or count < 0 or type(epoch) is not int or epoch < 0:
            raise ValueError('invalid policy runtime counters')
        if self._epoch is not None and (epoch != self._epoch or count < self._last_seen):
            self.reset_context()
        if self._epoch is None:
            sample = encode_snapshot(images, 0, self.params)
            self._epoch, self._baseline = epoch, count
            self._last_seen = self._last_submitted = count
            self._history.append(sample)
            return action_chunk
        self._last_seen = count
        if self._failure is not None:
            raise self._failure
        self._collect(runtime)
        if self._finished:
            return action_chunk
        if self.pending is None and count - self._last_submitted >= self.params['check_every_steps']:
            self._history.append(encode_snapshot(images, count - self._baseline, self.params))
            started = monotonic()
            self._request = {'generation': self._generation, 'epoch': epoch, 'step': count - self._baseline,
                             'history_size': len(self._history), 'started': started,
                             'deadline': started + self.params['provider']['timeout_s']}
            self.pending = self._executor.submit(self._query, tuple(self._history))
            self._last_submitted = count
        return action_chunk

    def _invalidate(self):
        self._generation += 1
        self._history.clear()
        self._epoch = self._baseline = self._last_seen = self._last_submitted = None
        self._finished = False
        self._failure = None

    def _drain(self):
        if self.pending is not None:
            try:
                self.pending.result()
            except Exception:
                pass  # Invalidated verdicts and errors belong to the old episode.
            self.pending = None
        self._request = None

    def reset_context(self):
        if self._closed:
            raise RuntimeError('llm_failure is closed')
        self._invalidate()
        self._drain()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._invalidate()
        try:
            self._drain()
            self.provider.close()
        finally:
            # As for SDK AsyncChunkPolicy, the host's absolute lifecycle deadline
            # terminates the complete process tree if a provider refuses to exit.
            self._executor.shutdown(wait=True, cancel_futures=True)
