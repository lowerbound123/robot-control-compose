"""Best-effort, credential-safe JSON lines shared by the two policy threads."""
import json
import math
import re
from threading import Lock


MAX_EVENT_BYTES = 8000
_PRINT_LOCK = Lock()
_SENSITIVE_KEYS = {'authorization', 'proxy-authorization', 'api_key', 'api-key', 'apikey', 'x-api-key'}
_AUTHORIZATION = re.compile(r'(?i)(\b(?:proxy-)?authorization\b["\']?\s*[:=]\s*)[^\r\n]*')


def _json(value):
    # Escapes embedded newlines/control characters, but preserves normal Unicode.
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    # JSON servers can legally escape isolated surrogates. Keep those escaped
    # instead of losing the entire event when encoding its UTF-8 output.
    return text.encode('utf-8', errors='backslashreplace').decode('utf-8')


def sanitize(value, secrets=()):
    """Copy JSON-compatible values, replacing credentials even in reflected bodies."""
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                for variant in (secret, json.dumps(secret)[1:-1], _json(secret)[1:-1]):
                    value = value.replace(variant, '[REDACTED]')
        return _AUTHORIZATION.sub(r'\1[REDACTED]', value)
    if isinstance(value, dict):
        return {sanitize(str(key), secrets): ('[REDACTED]' if str(key).lower() in _SENSITIVE_KEYS
                                             else sanitize(item, secrets))
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [sanitize(item, secrets) for item in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return f'<{type(value).__name__}>'


def sanitized_response(text, secrets=()):
    """Redact decoded JSON too, so escaped reflected credentials cannot leak."""
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return sanitize(text, secrets)
    return _json(sanitize(decoded, secrets))


def sanitized_metadata(metadata, secrets=()):
    value = sanitize({key: item for key, item in metadata.items() if key != 'raw_response'}, secrets)
    if 'raw_response' in metadata:
        raw = metadata['raw_response']
        value['raw_response'] = sanitized_response(raw, secrets) if isinstance(raw, str) else sanitize(raw, secrets)
    return value


def provider_metadata(model, provider='deepseek'):
    return {'provider': provider, 'model': model, 'response_model': None,
            'provider_request_id': None, 'http_status': None, 'usage': None,
            'finish_reason': None, 'raw_response': None, 'parsed_failed': None, 'error': None,
            'http_started_wall_s': None, 'http_started_mono_s': None,
            'http_finished_wall_s': None, 'http_finished_mono_s': None, 'http_duration_s': None}


def _strings(value, path=()):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*path, index))


def _set_path(value, path, item):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = item


def event_line(event, secrets=()):
    """Bound the serialized event, including escapes and the trailing newline."""
    value = sanitized_metadata(event, secrets)
    if isinstance(value.get('raw_response'), str):
        value['raw_response_original_size_bytes'] = len(value['raw_response'].encode('utf-8', errors='backslashreplace'))
    value['original_size_bytes'] = len(_json(value).encode('utf-8'))
    value['truncated'], value['truncated_fields'] = False, []
    line = _json(value)
    if len(line.encode('utf-8')) + 1 <= MAX_EVENT_BYTES:
        return line
    value['truncated'] = True
    # Provider data is held in strings or a small allowlisted token-usage object.
    # Retain each prefix as far as the final escaped JSON byte budget permits.
    strings = sorted(_strings(value), key=lambda item: len(_json(item[1]).encode('utf-8')), reverse=True)
    for path, original in strings:
        if path[0] in ('event', 'query_id', 'truncated_fields'):
            continue
        value['truncated_fields'].append('.'.join(map(str, path)))
        _set_path(value, path, '')
        if len(_json(value).encode('utf-8')) + 1 > MAX_EVENT_BYTES:
            continue
        low, high = 0, len(original)
        while low < high:
            middle = (low + high + 1) // 2
            _set_path(value, path, original[:middle])
            if len(_json(value).encode('utf-8')) + 1 <= MAX_EVENT_BYTES:
                low = middle
            else:
                high = middle - 1
        _set_path(value, path, original[:low])
        return _json(value)
    # Unexpected metadata must never produce a partial or unbounded JSON line.
    raise ValueError('llm_failure diagnostic metadata exceeds event budget')


class Diagnostics:
    def __init__(self, api_key, *other_keys):
        self.secrets = tuple(value for key in (api_key, *other_keys) for value in (key, key.strip()))

    def emit(self, event):
        try:
            line = event_line(event, self.secrets)
            with _PRINT_LOCK:
                print(line, flush=True)
        except Exception:
            # Diagnostic sinks never change verdicts, faults or cleanup behavior.
            pass
