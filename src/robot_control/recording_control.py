"""Local recording commands and bounded controller producers."""
import time

from .recording import RecordingService


RECORD_HELP = 'record：切换录制；record on/off：开始/停止；record status：状态、目录、采样、缺口和待写入量'


class RecordingControl:
    def _init_recording(self):
        self._record_global_version = None
        self._recorder = RecordingService(self.outputs/'logs', self.run_id, self._record_snapshot,
            on_error=self._record_error, stop_timeout=self.config.get('timeouts', {}).get('release_s', 30))
        if hasattr(self.robot, 'set_command_observer'):
            self.robot.set_command_observer(self._recorder.sent)
        self.operations.on_event = lambda event: self._recorder.event('events',
            {'event':'command.receipt', 'run_id':self.run_id, **event}, priority=True)

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
        try:
            if len(words) > 2:
                raise ValueError(RECORD_HELP)
            action = words[1] if len(words) == 2 else 'toggle'
            if action in ('toggle', 'on') and not self._recorder.active:
                self._record_global_version = None
            metadata = self._record_metadata() if action in ('toggle', 'on') else {}
            future = self._recorder.command(action, metadata)
            self.operations.update(identity, 'running')
        except Exception as exc:
            self.operations.update(identity, 'failed', error=exc)
            self._emit(f'录制命令失败：{exc}', 'error')
            return

        def finished(result):
            try:
                status = result.result()
                self.operations.update(identity, 'succeeded', result=status)
                self._emit(f"record {status['state']}；目录 {status['path']}；"
                    f"采样 {status['samples_written']}；重复帧 {status['repeated_frames']}；"
                    f"缺失帧 {status['missing_frames']}；采样缺口 {status['missed_samples']}；"
                    f"待写入 {status['pending_bytes']} bytes；错误 {status['error'] or '无'}")
            except Exception as exc:
                self.operations.update(identity, 'failed', error=exc)
                self._emit(f'录制命令失败：{exc}', 'error')
        future.add_done_callback(finished)
