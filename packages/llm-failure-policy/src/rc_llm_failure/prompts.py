"""Provider prompt validation shared by the worker and the offline migration."""
import re
from string import Formatter

from .decisions import validate_decision_fields


_INPUT_FIELDS = {'task': {'task'}, 'history_snapshot': {'index', 'step'},
                 'current_snapshot': {'index', 'step'}, 'camera': {'camera'}}
VARIABLE_FIELD = re.compile(r'\$([A-Za-z_][A-Za-z0-9_]*)')


def _valid_template(name, template):
    if not isinstance(template, str) or not template.strip():
        return False
    used = set()
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is None:
                continue
            if (field not in _INPUT_FIELDS[name] and not VARIABLE_FIELD.fullmatch(field)) or spec or conversion:
                return False
            used.add(field)
    except ValueError:
        return False
    return name != 'task' or 'task' in used


def validate_prompt_fields(settings, path, *, allow_empty_task=False):
    for name in ('task', 'prompt'):
        value = settings.get(name)
        if not isinstance(value, str) or (not value.strip() and not (allow_empty_task and name == 'task')):
            raise ValueError(f'{path}: {name} must be a nonempty string')
    if not isinstance(settings.get('response_instructions'), str):
        raise ValueError(f'{path}: response_instructions must be a string')
    templates = settings.get('input_templates')
    if not isinstance(templates, dict) or templates.keys() != _INPUT_FIELDS.keys():
        raise ValueError(f'{path}: input_templates requires {", ".join(_INPUT_FIELDS)}')
    for name, allowed in _INPUT_FIELDS.items():
        if not _valid_template(name, templates[name]):
            raise ValueError(f'{path}: invalid input_templates.{name}; '
                             f'use plain placeholders from {", ".join(sorted(allowed))}')
    validate_decision_fields(settings, path)
