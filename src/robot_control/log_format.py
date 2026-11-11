"""Shared log metadata formatting, without terminal/UI dependencies."""
from datetime import datetime, timedelta, timezone
from enum import Enum
import re

UTC8 = timezone(timedelta(hours=8))
_POLICY = re.compile(r'^\[([A-Za-z0-9][A-Za-z0-9_.-]*)\]\s')
_COMMAND = re.compile(r'^policy command \[([^\]:\r\n]+)(?::[^\]\r\n]*)?\]')
LEVELS = {'TRACE': 5, 'DEBUG': 10, 'INFO': 20, 'WARNING': 30, 'ERROR': 40, 'CRITICAL': 50}


class LogSource(Enum):
    DIAGNOSTIC = 'diagnostic'
    COMMAND = 'command'


def logging_settings(value):
    if not isinstance(value, dict) or set(value) - {'level'}:
        raise ValueError('logging 必须为只包含 level 的对象')
    level = value.get('level', 'INFO')
    if not isinstance(level, str) or level.upper() not in LEVELS:
        raise ValueError('logging.level 必须为 TRACE/DEBUG/INFO/WARNING/ERROR/CRITICAL')
    return {'level': level.upper()}


def format_log_time(value):
    """Use a fixed offset, independent of the host/container's timezone."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC8).isoformat(timespec='milliseconds')
        except (ValueError, OverflowError, OSError):
            pass  # A malformed old record must not stop the log viewer.
    return '' if value is None else str(value)


def level_label(level):
    """Four-column display labels; stored severity names remain unchanged."""
    level = str(level).upper()
    return {'WARNING': 'WARN', 'ERROR': 'ERRO', 'DEBUG': 'DBUG',
            'CRITICAL': 'CRIT', 'FATAL': 'FATL', 'TRACE': 'TRCE'}.get(level, level[:4].ljust(4))


def event_metadata(event):
    message = str(event.get('message', ''))
    policy = _POLICY.match(message) or _COMMAND.match(message)
    source = ('policy:' + policy.group(1) if policy else
              'MCP' if re.match(r'^mcp\b', message, re.IGNORECASE) else 'RC')
    if event.get('event') in ('policy.log', 'policy.command') and event.get('instance_id'):
        source = 'policy:' + str(event['instance_id'])
    # The display is always UTC+8; the persisted ISO timestamp keeps its offset.
    timestamp = format_log_time(event.get('time', '')).removesuffix('+08:00')
    return timestamp, str(event.get('level', 'INFO')).upper(), source
