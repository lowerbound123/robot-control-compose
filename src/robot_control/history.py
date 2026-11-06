"""Per-configuration command history; filesystem work stays on a daemon worker."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from rc_policy_sdk.command_syntax import parse_command
from .secret_config import reject_secret_edit


def valid_history_line(line):
    if not isinstance(line, str) or not line.strip() or '\n' in line or '\r' in line:
        raise ValueError('历史只保存非空单行命令')
    reject_secret_edit(line)
    parse_command(line)
    return line


def recent_unique(lines, limit=500):
    return list(dict.fromkeys(reversed(lines)))[:limit][::-1]


class CommandHistory:
    """Memory snapshots are immediate; load and coalesced atomic saves are asynchronous."""

    def __init__(self, config_path, outputs, *, limit=500):
        identity = os.path.abspath(os.fspath(config_path))
        digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]
        self.path = Path(outputs) / 'command-history' / (digest + '.json')
        self.limit = limit
        self._condition = threading.Condition()
        self._lines = []
        self._revision = self._saved_revision = 0
        self._loaded = self._dirty = self._stopping = False
        self._warning = None
        self._thread = threading.Thread(target=self._run, name='rc-command-history', daemon=True)
        self._thread.start()

    def snapshot(self):
        with self._condition:
            return tuple(self._lines), self._revision

    def record(self, line):
        valid_history_line(line)
        with self._condition:
            if self._stopping:
                return
            self._lines = recent_unique([*self._lines, line], self.limit)
            self._revision += 1
            self._dirty = True
            self._condition.notify_all()

    def take_warning(self):
        with self._condition:
            warning, self._warning = self._warning, None
            return warning

    def wait_idle(self, timeout=None):
        """Optional synchronization for callers outside the UI lifecycle."""
        with self._condition:
            return self._condition.wait_for(
                lambda: self._loaded and not self._dirty and self._saved_revision >= self._revision,
                timeout)

    def close(self):
        """Request a final background save without delaying controller cleanup."""
        with self._condition:
            self._stopping = True
            self._condition.notify_all()

    def _load(self):
        try:
            if self.path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('history too large')
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(payload, dict) or payload.get('version') != 1 or not isinstance(payload.get('commands'), list):
                raise ValueError('invalid history format')
            lines = []
            for line in payload['commands']:
                valid_history_line(line)
                lines.append(line)
            return recent_unique(lines, self.limit)
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError, RecursionError):
            with self._condition:
                self._warning = '命令历史无法读取；本次仍可正常输入。'
            return []

    def _save(self, lines):
        temporary = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix='.' + self.path.stem + '-', dir=self.path.parent)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({'version': 1, 'commands': lines}, stream, ensure_ascii=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _run(self):
        loaded = self._load()
        with self._condition:
            self._lines = recent_unique([*loaded, *self._lines], self.limit)
            self._revision += 1
            self._loaded = True
            if not self._dirty:
                self._saved_revision = self._revision
            self._condition.notify_all()
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._dirty or self._stopping)
                if not self._dirty:
                    return
                lines, revision = list(self._lines), self._revision
                self._dirty = False
            try:
                self._save(lines)
            except (OSError, ValueError, TypeError):
                with self._condition:
                    self._warning = '命令历史无法保存；本次仍可正常输入。'
            with self._condition:
                self._saved_revision = revision
                self._condition.notify_all()
