"""Provider prompt validation shared by the worker and the offline migration."""
from string import Formatter


_INPUT_FIELDS = {'task': {'task'}, 'history_snapshot': {'index', 'step'},
                 'current_snapshot': {'index', 'step'}, 'camera': {'camera'}}


def _valid_template(name, template):
    if not isinstance(template, str) or not template.strip():
        return False
    used = set()
    try:
        for _, field, spec, conversion in Formatter().parse(template):
            if field is None:
                continue
            if field not in _INPUT_FIELDS[name] or spec or conversion:
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
    if settings.get('type') == 'openai_decisions':
        choices = settings.get('choice_descriptions')
        if (not isinstance(choices, dict) or choices.keys() != {'failed', 'not_failed'}
                or not all(isinstance(value, str) and value.strip() for value in choices.values())):
            raise ValueError(f'{path}: choice_descriptions requires text for failed and not_failed')
    elif 'choice_descriptions' in settings:
        raise ValueError(f'{path}: choice_descriptions is only supported by openai_decisions')
