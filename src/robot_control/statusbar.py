"""Pure formatting for the persistent operator statusbar."""

_FIELDS = (
    'state', 'policy', 'control_owner', 'in_flight', 'consumed', 'last_action_request',
    'runtime', 'target', 'runtime_error', 'recording_state', 'recording_error',
    'connector_source', 'connector_config',
)


def _display(value):
    return '—' if value is None or value == '' else str(value)


def format_statusbar(snapshot):
    """Keep every whitelist chunk, preserving literal paths and full errors."""
    fields = []
    for key in _FIELDS:
        value = snapshot.get(key)
        if key == 'last_action_request':
            request = value or {}
            fields.append(f"请求 {_display(request.get('request_id'))}："
                          f"裁剪 {_display(request.get('trim'))}，缓存 {_display(request.get('cached'))}")
        else:
            text = _display(value)
            if key in ('runtime_error', 'recording_error'):
                text = ' '.join(text.splitlines())
                if not text.strip():
                    text = '—'
            fields.append(f'{key}={text}')
    return ' | '.join(fields)
