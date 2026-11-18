"""RC-owned queue consumption with opt-in inclusive prefetch and closest-point fusion."""
import time
from urllib.parse import urlsplit

from rc_policy_sdk.monitored_chunks import MonitoredAsyncChunkPolicy

from .gripper import gripper_rules, process_grippers


EMPTY_CHUNK_METRICS = dict(left_gripper_raw='[]', right_gripper_raw='[]',
                           left_gripper_processed='[]', right_gripper_processed='[]',
                           last_request_bytes=0, server_latency_ms='—')


class OpenWAMPolicy(MonitoredAsyncChunkPolicy):
    def __init__(self, config):
        from .backend import OpenWAMBackend
        self.params = config['params']
        self.config_version = None
        # A strict SDK threshold of 1 handles only the empty queue. This policy
        # handles a positive, inclusive threshold after settling the current chunk.
        super().__init__(OpenWAMBackend(self.params, self.prediction_stage), instruction=self.params['task'], chunk_size=32,
                         request_when_remaining=1, inference_timeout_s=self.params['inference_timeout_s'],
                         closest_point_alignment=True)
        endpoint = urlsplit(self.params['service_url'])
        self._record(service_origin=f'{endpoint.scheme}://{endpoint.netloc}'[:512], protocol_version=2,
                     request_when_remaining=self.params['request_when_remaining'], **EMPTY_CHUNK_METRICS)

    def _launch(self, state, images, runtime):
        self._rules = gripper_rules(self.params)
        super()._launch(state, images, runtime)

    def _predict(self, qpos, images, instruction):
        actions, _ = super()._predict(qpos, images, instruction)
        self.prediction_stage('post')
        processed = process_grippers(actions, **self._rules)
        self._record(left_gripper_raw=str(actions[:, 6].tolist()), right_gripper_raw=str(actions[:, 13].tolist()),
                     left_gripper_processed=str(processed[:, 6].tolist()),
                     right_gripper_processed=str(processed[:, 13].tolist()),
                     last_request_bytes=self.backend.last_request_bytes, server_latency_ms=self.backend.server_latency_ms)
        return processed, time.monotonic()

    def step(self, state, action_chunk, images, *, runtime):
        if self._closed:
            raise RuntimeError('policy is closed')
        if self._failure is not None:
            raise self._failure
        version = runtime.config_version
        task_changed = self.instruction != self.params['task']
        if self._epoch is not None and self._epoch != runtime.control_epoch:
            if runtime.control_epoch < self._epoch or version <= self.config_version:
                raise RuntimeError('control epoch changed without reset or a newer configuration')
            if len(action_chunk) and not task_changed:
                raise RuntimeError('new configuration epoch must discard the old action chunk')
            self.reset()
        elif task_changed:
            self.reset()
        self.config_version = version
        old = [] if task_changed else action_chunk
        if (self.pending is not None and not len(old) and
                self._request == (runtime.consumed_actions - self.chunk_size, runtime.control_epoch)):
            # All 32 predicted rows expired while RC was consuming the previous
            # chunk. Check the reply/deadline before requesting a fresh snapshot.
            self._await_prediction()
            self._drain()
            with self._metrics_lock:
                self._metrics['trimmed_actions_total'] += self.chunk_size
        remaining = super().step(state, old, images, runtime=runtime)
        if self.pending is None and 0 < len(remaining) <= self.params['request_when_remaining']:
            self._launch(state, images, runtime)
        return remaining

    def reset(self):
        if self.pending is not None:
            # Interrupt recv before draining the executor. A late reply stays
            # on the old socket; the next request uses a newly verified session.
            self.backend.close()
        super().reset()
        self.config_version = None
        self.instruction = self.params['task']
        self._record(task=self.instruction[:512], request_when_remaining=self.params['request_when_remaining'],
                     **EMPTY_CHUNK_METRICS)

    def close(self):
        self.backend.close()
        super().close()
