"""Single-flight model checks; only the policy callback can report a verdict."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import local
from time import monotonic, time
from uuid import uuid4

from rc_policy_sdk.actions import normalize_actions

from .configuration import validate_config, selected_provider
from .diagnostics import Diagnostics, provider_metadata, sanitize
from .errors import ProviderError
from .images import encode_snapshot


class FailurePolicy:
    def __init__(self, config, *, provider=None):
        self.params = validate_config(config)
        self.instance_id = config['id']
        self.control_mode = config['execution']['control_mode']
        self._provider_settings = selected_provider(self.params)
        self._provider_label = 'GPT Decisions' if self._provider_settings['type'] == 'openai_decisions' else 'DeepSeek'
        self._diagnostics = Diagnostics(self.params['provider']['api_key'], self.params['decision_api']['api_key'])
        self._diagnostic_context = local()
        if provider is None:
            from .providers import ProviderClient
            provider = ProviderClient(self._provider_settings, diagnostics=self._provider_diagnostics)
        self.provider = provider
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='llm-failure')
        self._history = deque(maxlen=self.params['history_size'])
        self.pending = None
        self._request = None
        self._generation = 0
        self._epoch = self._baseline = self._last_seen = self._last_submitted = None
        self._finished = self._closed = False
        self._failure = None

    def _provider_diagnostics(self, metadata):
        current = getattr(self._diagnostic_context, 'current', None)
        if current is not None:
            current.update(metadata)

    def _event(self, request, event, **extra):
        metadata = dict(request['diagnostics'])
        metadata.update(event=event, invalidated=(request['generation'] != self._generation
                                                 or request.get('discard_reason') is not None),
                        **extra)
        self._diagnostics.emit(metadata)

    def _collection_event(self, request, event, **extra):
        collected = monotonic()
        finished = request['diagnostics']['finished_mono_s']
        self._event(request, event, collected_wall_s=time(), collected_mono_s=collected,
                    collection_wait_s=None if finished is None else max(0., collected - finished), **extra)

    def _query(self, request, snapshots):
        metadata = request['diagnostics']
        metadata.update(started_wall_s=time(), started_mono_s=monotonic())
        self._diagnostic_context.current = metadata
        self._event(request, 'llm_failure.request.started')
        try:
            failed = self.provider.decide(self.params['task'], self.params['prompt'], snapshots)
            if type(failed) is not bool:
                raise ProviderError('供应商响应不符合失败判断契约')
            metadata['parsed_failed'] = failed
        except ProviderError as exc:
            metadata['error'] = sanitize(str(exc), self._diagnostics.secrets)
            raise ProviderError(metadata['error']) from None
        except Exception:
            metadata['error'] = f'{self._provider_label} 检查异常'
            raise ProviderError(metadata['error']) from None
        except BaseException:
            metadata['error'] = f'{self._provider_label} 检查被中断'
            raise
        finally:
            completed = monotonic()
            metadata.update(finished_wall_s=time(), finished_mono_s=completed,
                            elapsed_s=completed - metadata['started_mono_s'], late=completed > request['deadline'])
            self._event(request, 'llm_failure.request.error' if metadata['error'] else 'llm_failure.request.completed')
            self._diagnostic_context.current = None
        return failed, completed

    def _collect(self, runtime):
        if self.pending is None:
            return
        request = self._request
        if not self.pending.done():
            if monotonic() >= request['deadline']:
                self._failure = ProviderError(f'{self._provider_label} 检查超时')
                if not request.get('timeout_reported'):
                    request['timeout_reported'] = True
                    request['discard_reason'] = 'timeout'
                    self._collection_event(request, 'llm_failure.collection.timeout', pending=True,
                                           reason='deadline_exceeded', error=str(self._failure))
                raise self._failure
            return
        future, self.pending = self.pending, None
        self._request = None
        if request['generation'] != self._generation or request['epoch'] != runtime.control_epoch:
            self._collection_event(request, 'llm_failure.collection.discarded',
                                   reason='generation_changed' if request['generation'] != self._generation else 'epoch_changed')
            return
        try:
            failed, completed = future.result()
        except ProviderError as exc:
            self._failure = exc
            self._collection_event(request, 'llm_failure.collection.discarded', reason='provider_error')
            raise
        if completed > request['deadline']:
            self._failure = ProviderError(f'{self._provider_label} 检查超时')
            self._collection_event(request, 'llm_failure.collection.discarded', reason='deadline_exceeded',
                                   error=str(self._failure))
            raise self._failure
        self._collection_event(request, 'llm_failure.collection.accepted')
        if failed:
            runtime.finish(f'llm_failure：{self._provider_label} 判断任务已进入失败状态', outcome='terminated')
            self._finished = True

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
                             'deadline': started + self._provider_settings['timeout_s']}
            self._request['diagnostics'] = {
                **provider_metadata(self._provider_settings['model'], self._provider_settings['type']),
                'query_id': uuid4().hex, 'instance_id': self.instance_id,
                'step': self._request['step'], 'history_size': self._request['history_size'],
                'history_steps': [sample.step for sample in self._history],
                'generation': self._generation, 'epoch': epoch,
                'submitted_wall_s': time(), 'submitted_mono_s': started,
                'deadline_mono_s': self._request['deadline'],
                'started_wall_s': None, 'started_mono_s': None,
                'finished_wall_s': None, 'finished_mono_s': None, 'elapsed_s': None,
                'collection_wait_s': None, 'late': False}
            self.pending = self._executor.submit(self._query, self._request, tuple(self._history))
            self._last_submitted = count
        return action_chunk

    def _invalidate(self, reason):
        if self._request is not None:
            self._request.setdefault('discard_reason', reason)
        self._generation += 1
        self._history.clear()
        self._epoch = self._baseline = self._last_seen = self._last_submitted = None
        self._finished = False
        self._failure = None

    def _drain(self):
        if self.pending is not None:
            request = self._request
            try:
                self.pending.result()
            except Exception:
                pass  # Invalidated verdicts and errors belong to the old episode.
            finally:
                self._collection_event(request, 'llm_failure.collection.discarded',
                                       reason=request.get('discard_reason', 'drain'))
                self.pending = None
        self._request = None

    def reset_context(self):
        if self._closed:
            raise RuntimeError('llm_failure is closed')
        self._invalidate('reset')
        self._drain()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._invalidate('close')
        try:
            self._drain()
            self.provider.close()
        finally:
            # As for SDK AsyncChunkPolicy, the host's absolute lifecycle deadline
            # terminates the complete process tree if a provider refuses to exit.
            self._executor.shutdown(wait=True, cancel_futures=True)
