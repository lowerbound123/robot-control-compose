"""Background log reduction; no encoding, filesystem scan or waits on the UI thread."""
from collections import deque
from concurrent.futures import CancelledError, Future
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def validate_target(name):
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or '/' in name or '\\' in name or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError('record reduce 需要日志文件夹名或 all，不能使用路径')
    return name


def log_directories(root, prefix, *, limit=200, timeout=1):
    if prefix:
        validate_target(prefix)
    deadline, items, truncated = time.monotonic()+timeout, [], False
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= 4096 or time.monotonic() >= deadline:
                truncated = True
                break
            if entry.name.startswith(prefix) and entry.is_dir(follow_symlinks=False):
                try:
                    validate_target(entry.name)
                except ValueError:
                    continue
                items.append({'text': entry.name, 'name': entry.name, 'kind': 'log'})
                if len(items) > limit:
                    truncated = True
                    break
    return {'items': sorted(items, key=lambda row: row['text'])[:limit], 'truncated': truncated}


class ReductionFailed(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


class RecordingReducer:
    def __init__(self, directory, *, on_log=None):
        self.directory = Path(directory)
        self.on_log = on_log or (lambda line: None)
        self._lock = threading.Lock()
        self._closed = False
        self._thread = self._process = self._future = None

    def submit(self, target):
        validate_target(target)
        with self._lock:
            if self._closed:
                raise RuntimeError('日志缩减服务正在关闭')
            if self._future is not None and not self._future.done():
                raise RuntimeError('record reduce 正在执行，请等待当前任务完成')
            future = self._future = Future()
            self._thread = threading.Thread(target=self._run, args=(target, future),
                                            name='record-reduce', daemon=True)
            self._thread.start()
            return future

    def _run(self, target, future):
        process = None
        result, error = None, None
        try:
            from .recording_videos import real_directory
            real_directory(self.directory)
            root = self.directory.resolve()
            selected = root if target == 'all' else root / target
            real_directory(selected)
            if target != 'all' and selected.resolve().parent != root:
                raise ValueError('日志目录不在当前 logs 目录内')
            command = [sys.executable, '-u', str(Path(__file__).with_name('recording_videos.py')),
                       str(selected), '--json-summary']
            with self._lock:
                if self._closed:
                    raise CancelledError('RC 退出，日志缩减已取消')
                process = self._process = subprocess.Popen(command, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', start_new_session=True)
            summary, errors = None, deque(maxlen=8)
            for line in process.stdout:
                line = line.rstrip()
                if line.startswith('{'):
                    try:
                        value = json.loads(line)
                    except ValueError:
                        value = None
                    if isinstance(value, dict) and value.get('type') == 'record.reduce.summary':
                        summary = {key: value[key] for key in ('converted', 'planned', 'skipped', 'failed')}
                        continue
                if line.startswith('[ERROR]'):
                    errors.append(line)
                self.on_log(line)
            returncode = process.wait()
            with self._lock:
                if self._closed:
                    raise CancelledError('RC 退出，日志缩减已取消；未完成转换的原图保留')
            result = {'target': target, 'path': str(selected), **(summary or {})}
            if returncode or summary is None:
                raise ReductionFailed('\n'.join(errors) or f'日志缩减进程退出码 {returncode}，没有完整成功汇总', result)
        except BaseException as exc:
            error = exc
        finally:
            if process is not None:
                if process.poll() is None:
                    self._signal(process, signal.SIGKILL)
                    process.wait()
                process.stdout.close()
            with self._lock:
                if self._process is process:
                    self._process = None
        # Publish only after the child is reaped, so the next accepted job
        # cannot overlap the preceding job's encoder or file cleanup.
        if error is None:
            future.set_result(result)
        else:
            future.set_exception(error)

    @staticmethod
    def _signal(process, signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    def close(self, deadline):
        with self._lock:
            self._closed = True
            process, thread = self._process, self._thread
            if process is not None and process.poll() is None:
                self._signal(process, signal.SIGTERM)
        if thread is None:
            return False
        thread.join(max(0., min(3., deadline-time.monotonic())))
        forced = thread.is_alive()
        if forced:
            if process is not None:
                self._signal(process, signal.SIGKILL)
            thread.join(max(0., deadline-time.monotonic()))
        if thread.is_alive():
            raise RuntimeError('日志缩减线程未在退出期限内结束')
        return forced
