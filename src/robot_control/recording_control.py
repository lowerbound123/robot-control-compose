"""Local recording commands and bounded controller producers."""
import copy
from concurrent.futures import CancelledError
import time

from .recording import RecordingService
from .recording_reduce import RecordingReducer, ReductionFailed
from .recording_auto import RecordingAuto


RECORD_HELP = 'record：切换录制；record start：开始；record stop：停止；record auto [on|off]：自动换段；record status：状态；record reduce <日志文件夹名|all>：缩减静态录制的体积'


class RecordingControl(RecordingAuto):
    def _init_recording(self):
        self._record_global_version = None
        self._record_requested = False
        self._init_record_auto()
        self._recorder = RecordingService(self.outputs/'logs', self.run_id, self._record_snapshot,
            on_error=self._record_error, stop_timeout=self.config.get('timeouts', {}).get('release_s', 30))
        self._record_reducer = RecordingReducer(self.outputs/'logs',
            on_log=lambda line: self._emit_command('record reduce: ' + line,
                                                  'error' if line.startswith('[ERROR]') else 'info'))
        if hasattr(self.robot, 'set_command_observer'):
            self.robot.set_command_observer(self._recorder.sent)
        self.operations.on_event = self._record_operation_event

    def _record_error(self, message):
        self.exit_code = 1
        self._emit(f'录制/日志写入错误：{message}', 'error')

    def _record_snapshot(self):
        # Recorder thread reads cached robot values; it never acquires a policy
        # image lease or waits for the controller's lifecycle lock.
        snapshot = self.robot.recording_snapshot()
        service = self._global_service
        if service is not None and service.version != self._record_global_version:
            try:
                version, value = service.snapshot()
                if version != self._record_global_version:
                    if self._recorder.event('configs', {'identity':f'globals:{version}',
                            'kind':'globals', 'version':version, 'value':value}):
                        self._record_global_version = version
            except Exception:
                pass
        return snapshot

    def _record_metadata(self):
        with self._lock:
            record = self.registry.get(self._identity) if self._identity else None
            members = record.members if record and record.is_chain else (record,) if record else ()
            data = {'run_id':self.run_id, 'timestamp_ns':time.time_ns(),
                    'connector': self.connector_status(),
                    'safety': copy.deepcopy(self._effective_safety),
                    'rc':self.config, 'policy':self._identity,
                    'generation':self.buffer.generation, 'control_epoch':self._control_epoch,
                    'control_owner':self.control_owner,
                    'instances':[{'instance_id':m.instance_id, 'source_id':m.source_id,
                                  'version':m.version, 'directory':str(m.directory),
                                  'config':m.config} for m in members],
                    'runtime':self.runtime.snapshot() if self.runtime else None}
        return data

    def _record_configuration(self):
        if self._recorder.active:
            data = self._record_metadata()
            data['identity'] = f"{self._identity}:{self.buffer.generation}:" + ','.join(
                f"{m['instance_id']}:{m['version']}" for m in data['instances'])
            self._recorder.event('configs', data, priority=True)

    def _submit_recording(self, identity, words):
        auto_start = False
        try:
            if self._stop.is_set():
                raise RuntimeError('controller is shutting down')
            if len(words) == 3 and words[1] == 'reduce':
                self.operations.update(identity, 'running')
                future = self._record_reducer.submit(words[2])
                future.add_done_callback(lambda result: self._reduction_finished(identity, result))
                return
            if words[1:2] == ['auto']:
                if words[2:] not in ([], ['on'], ['off']):
                    raise ValueError(RECORD_HELP)
                enabled = not self._record_auto if len(words) == 2 else words[2] == 'on'
                self._set_record_auto(enabled)
                if not enabled:
                    self.operations.update(identity, 'succeeded', result=self._record_status())
                    self._emit_command('record auto off；保留当前录制')
                    return
                auto_start = True
                words = ['record', 'on']
            if len(words) > 2:
                raise ValueError(RECORD_HELP)
            action = words[1] if len(words) == 2 else 'toggle'
            if action in ('toggle', 'on', 'off'):
                if action == 'toggle':
                    transitioning = (self._connector_record_transition or self._record_auto_current is not None
                        or self._record_auto_queue or self._connector_retry_available and not self._connected)
                    self._record_requested = (not self._record_requested if transitioning else
                                              self._recorder.status()['state'] not in ('starting', 'recording'))
                else:
                    self._record_requested = action == 'on'
                action = 'on' if self._record_requested else 'off'
                if action == 'off': self._record_manual_stop()
            if (self._connector_record_transition or (self._connector_retry_available and not self._connected)) and action in ('toggle', 'on', 'off'):
                self._connector_record_wanted = self._record_requested
                if self._connector_record_wanted:
                    self.operations.update(identity, 'succeeded', result={**self._record_status(), 'requested': 'on', 'deferred_until_connector_ready': True})
                    return
                action = 'off'
            if action in ('toggle', 'on') and not self._recorder.active:
                self._record_global_version = None
            metadata = self._record_metadata() if action in ('toggle', 'on') else {}
            intent = (self._record_stop_epoch, self._record_auto_epoch)
            error_serial = self._record_auto_error_serial
            if auto_start and (self._recorder.status()['state'] == 'stopping' or self._record_auto_current is not None):
                future = self._record_auto_ensure(identity)
            else:
                future = self._recorder.command(action, metadata)
            self.operations.update(identity, 'running')
        except Exception as exc:
            if auto_start: self._record_auto_failed(exc)
            self.operations.update(identity, 'failed', error=exc)
            self._emit_command(f'录制命令失败：{exc}', 'error')
            return

        def finished(result):
            try:
                status = result.result()
                with self._lock:
                    if (action == 'on' and intent == (self._record_stop_epoch, self._record_auto_epoch)
                            and error_serial == self._record_auto_error_serial):
                        self._record_auto_error = None
                    status = self._record_status(status)
                self.operations.update(identity, 'succeeded', result=status)
                self._emit_command(f"record {status['state']}；目录 {status['path']}；"
                    f"采样 {status['samples_written']}；重复帧 {status['repeated_frames']}；"
                    f"缺失帧 {status['missing_frames']}；采样缺口 {status['missed_samples']}；"
                    f"待写入 {status['pending_bytes']} bytes；错误 {status['error'] or '无'}")
            except CancelledError as exc:
                self.operations.update(identity, 'cancelled', reason=str(exc))
                self._emit_command(f'录制命令已取消：{exc}', 'warning')
            except Exception as exc:
                with self._lock:
                    if auto_start and intent == (self._record_stop_epoch, self._record_auto_epoch):
                        self._record_auto_failed(exc)
                self.operations.update(identity, 'failed', error=exc)
                self._emit_command(f'录制命令失败：{exc}', 'error')
        future.add_done_callback(finished)

    def _reduction_finished(self, identity, future):
        try:
            result = self._record_status(future.result())
            self.operations.update(identity, 'succeeded', result=result)
            self._emit_command(f"record reduce 完成：{result['target']}；转换 {result['converted']}；"
                               f"跳过 {result['skipped']}；失败 {result['failed']}")
        except CancelledError as exc:
            self.operations.update(identity, 'cancelled', reason=str(exc))
            self._emit_command(str(exc), 'warning')
        except Exception as exc:
            self.operations.update(identity, 'failed', error=exc,
                                   result=self._record_status(exc.result) if isinstance(exc, ReductionFailed)
                                   and exc.result is not None else None)
            self._emit_command(f'record reduce 失败：{exc}', 'error')
