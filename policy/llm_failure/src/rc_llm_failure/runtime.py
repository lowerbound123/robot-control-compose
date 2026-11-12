"""Single-flight model checks; only the policy callback can report a verdict."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Lock, local
from time import monotonic, time
from math import isfinite
from uuid import uuid4

from rc_policy_sdk.actions import normalize_actions

from .configuration import validate_config
from .diagnostics import Diagnostics, provider_metadata, sanitize
from .errors import LocalPolicyError, ProviderError
from .images import CAMERAS, CheckInput, encode_snapshot
from .prompt_variables import PromptTemplates
from .prompts import validate_prompt_fields


class FailurePolicy:
    def __init__(self, config, provider_settings, *, provider=None):
        self.params = validate_config(config)
        self.instance_id = config['id']
        self.control_mode = config['execution']['control_mode']
        self._provider_settings = provider_settings
        self._provider_label = 'DeepSeek' if provider_settings['type'] == 'deepseek' else 'GPT Decisions'
        self._diagnostics = Diagnostics(provider_settings['api_key'])
        self._diagnostic_context = local()
        if provider is None:
            from .providers import ProviderClient
            provider = ProviderClient(self._provider_settings, diagnostics=self._provider_diagnostics)
            self._prompts = provider.prompts
        else:
            validate_prompt_fields(provider_settings, 'provider')
            self._prompts = PromptTemplates(provider_settings)
        self.provider = provider
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='llm-failure')
        self._history = deque(maxlen=self.params['history_size'])
        self.pending = None
        self._request = None
        self._generation = 0
        self._metrics_lock = Lock()
        self._in_flight = 0
        self._reset_metrics()
        self._epoch = self._baseline = self._last_seen = self._last_submitted = None
        self._finished = self._closed = False
        self._failure = None

    def _reset_metrics(self):
        # Caller owns the small metrics lock once worker threads exist.
        self._failed_requests = 0
        self._http_count = self._token_count = 0
        self._http_sum = self._http_max = 0.
        self._token_sum = self._token_max = 0

    def monitor(self):
        """Six cached scalars; never waits for provider, output or lifecycle work."""
        with self._metrics_lock:
            return {
                'in_flight': self._in_flight,
                'avg_http_s': self._http_sum / self._http_count if self._http_count else '—',
                'max_http_s': self._http_max if self._http_count else '—',
                'avg_total_tokens': self._token_sum / self._token_count if self._token_count else '—',
                'max_total_tokens': self._token_max if self._token_count else '—',
                'failed_requests': self._failed_requests,
            }

    def _mark_failed(self, request):
        # Used both by the callback deadline check and worker completion.
        with self._metrics_lock:
            self._mark_failed_locked(request)

    def _mark_failed_locked(self, request):
        if request['generation'] == self._generation and not request.get('failure_counted'):
            request['failure_counted'] = True
            self._failed_requests += 1

    def _publish_completion(self, request, supplier_error, local_error):
        # Publish the provider outcome before diagnostic output can block. The
        # timestamp, not Future.done(), determines whether its verdict is late.
        completed = monotonic()
        metadata = request['diagnostics']
        with self._metrics_lock:
            metadata.update(finished_wall_s=time(), finished_mono_s=completed,
                            elapsed_s=completed - metadata['started_mono_s'], late=completed > request['deadline'])
            if request['generation'] == self._generation:
                if local_error or (not supplier_error and not metadata['late']):
                    # A callback may have provisionally counted a deadline
                    # before the actual completion/outcome was published.
                    if request.pop('failure_counted', False):
                        self._failed_requests -= 1
                else:
                    self._mark_failed_locked(request)
            if not metadata['late']:
                request.pop('timeout_reported', None)
                if request.get('discard_reason') == 'timeout':
                    request.pop('discard_reason')
        return completed

    def _complete_metrics(self, request):
        metadata = request['diagnostics']
        with self._metrics_lock:
            self._in_flight -= 1
            if request['generation'] != self._generation:
                return
            duration = metadata['http_duration_s']
            if type(duration) in (int, float) and isfinite(duration) and duration >= 0:
                self._http_count += 1
                self._http_sum += duration
                self._http_max = max(self._http_max, duration)
            usage = metadata['usage']
            tokens = usage.get('total_tokens') if isinstance(usage, dict) else None
            if type(tokens) is int and tokens >= 0:
                self._token_count += 1
                self._token_sum += tokens
                self._token_max = max(self._token_max, tokens)

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
        supplier_error = local_error = False
        try:
            failed = self.provider.decide(snapshots)
            if type(failed) is not bool:
                raise ProviderError('供应商响应不符合失败判断契约')
            metadata['parsed_failed'] = failed
        except ProviderError as exc:
            supplier_error = True
            metadata['error'] = sanitize(str(exc), self._diagnostics.secrets)
            raise ProviderError(metadata['error']) from None
        except LocalPolicyError as exc:
            local_error = True
            metadata['error'] = sanitize(str(exc), self._diagnostics.secrets)
            raise LocalPolicyError(metadata['error']) from None
        except Exception:
            local_error = True
            metadata['error'] = f'{self._provider_label} 检查异常'
            raise RuntimeError(metadata['error']) from None
        except BaseException:
            local_error = True
            metadata['error'] = f'{self._provider_label} 检查被中断'
            raise
        finally:
            completed = self._publish_completion(request, supplier_error, local_error)
            self._event(request, 'llm_failure.request.error' if metadata['error'] else 'llm_failure.request.completed')
            self._diagnostic_context.current = None
            self._complete_metrics(request)
        return failed, completed

    def _collect(self, runtime):
        if self.pending is None:
            return
        request = self._request
        if not self.pending.done():
            report_timeout = False
            with self._metrics_lock:
                if request['diagnostics']['finished_mono_s'] is None and monotonic() >= request['deadline']:
                    self._mark_failed_locked(request)
                    if not request.get('timeout_reported'):
                        request['timeout_reported'] = True
                        request['discard_reason'] = 'timeout'
                        report_timeout = True
            if report_timeout:
                self._collection_event(request, 'llm_failure.collection.timeout', pending=True,
                                       reason='deadline_exceeded', error=f'{self._provider_label} 检查超时')
            return
        future, self.pending = self.pending, None
        self._request = None
        if request['generation'] != self._generation or request['epoch'] != runtime.control_epoch:
            self._collection_event(request, 'llm_failure.collection.discarded',
                                   reason='generation_changed' if request['generation'] != self._generation else 'epoch_changed')
            return
        try:
            failed, completed = future.result()
        except ProviderError:
            self._collection_event(request, 'llm_failure.collection.discarded', reason='provider_error')
            return
        except Exception as exc:
            self._failure = exc
            self._collection_event(request, 'llm_failure.collection.discarded', reason='local_error')
            raise
        if completed > request['deadline']:
            self._mark_failed(request)
            error = f'{self._provider_label} 检查超时'
            self._collection_event(request, 'llm_failure.collection.discarded', reason='deadline_exceeded',
                                   error=error)
            return
        self._collection_event(request, 'llm_failure.collection.accepted')
        if failed:
            runtime.finish(f'llm_failure：{self._provider_label} 判断任务已进入失败状态', outcome='terminated')
            self._finished = True

    def _sample(self, images, step, state, runtime, *, submitting):
        values = self._prompts.sample_values(state if state is not None else {}, runtime, submitting=submitting)
        text = self._prompts.request_text(values) if submitting else None
        history, current = self._prompts.frame_texts(values, step, CAMERAS, include_current=submitting)
        sample = encode_snapshot(images, step, self.params, captions=history)
        self._history.append(sample)
        if text is not None:
            # Both views share owned JPEGs and camera labels. Only this request needs the current caption.
            frames = list(self._history)
            frames[-1] = replace(sample, captions=current)
            return CheckInput(tuple(frames), text)

    def policy(self, action_chunk, images, runtime, *, state=None):
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
            self._sample(images, 0, state, runtime, submitting=False)
            self._epoch, self._baseline = epoch, count
            self._last_seen = self._last_submitted = count
            return action_chunk
        self._last_seen = count
        if self._failure is not None:
            raise self._failure
        self._collect(runtime)
        if self._finished:
            return action_chunk
        if self.pending is None and count - self._last_submitted >= self.params['check_every_steps']:
            inputs = self._sample(images, count - self._baseline, state, runtime, submitting=True)
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
            with self._metrics_lock:
                self._in_flight += 1
            try:
                self.pending = self._executor.submit(self._query, self._request, inputs)
            except BaseException:
                with self._metrics_lock:
                    self._in_flight -= 1
                raise
            self._last_submitted = count
        return action_chunk

    def _invalidate(self, reason):
        if self._request is not None:
            self._request.setdefault('discard_reason', reason)
        with self._metrics_lock:
            self._generation += 1
            self._reset_metrics()
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
