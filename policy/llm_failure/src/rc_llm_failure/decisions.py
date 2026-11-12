"""Single-question Decisions profiles and their local failure rules."""
from .errors import ProviderError


DECISION_FIELDS = {
    'gpt-decision-predicate': {'question_name', 'fail_if'},
    'gpt-decision-choice': {'question_name', 'choices'},
    'gpt-decision-score': {'question_name', 'levels', 'fail_if'},
}
DECISION_TYPES = {name: name.removeprefix('gpt-decision-') for name in DECISION_FIELDS}
_PROFILE_FIELDS = {'choice_descriptions'}.union(*DECISION_FIELDS.values())
_CONTRACT_ERROR = 'GPT Decisions 响应不符合失败判断契约'


def _number_in_range(value, maximum):
    # Both comparisons reject NaN and infinities without casting large integers.
    return type(value) in (int, float) and 0 <= value <= maximum


def _entries(settings, path, field, limit, required):
    entries = settings[field]
    if not isinstance(entries, list) or not 2 <= len(entries) <= limit:
        raise ValueError(f'{path}: {field} must have 2 to {limit} entries')
    allowed = required | {'description'}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not required <= entry.keys() <= allowed:
            raise ValueError(f'{path}: invalid fields in {field}[{index}]')
        if 'description' in entry and not isinstance(entry['description'], str):
            raise ValueError(f'{path}: {field}[{index}].description must be a string')
    return entries


def validate_decision_fields(settings, path):
    kind = settings.get('type')
    if kind != 'deepseek' and (not isinstance(kind, str) or kind not in DECISION_TYPES):
        raise ValueError(f'{path}: missing or unsupported provider type')
    supplied = _PROFILE_FIELDS.intersection(settings)
    if supplied != DECISION_FIELDS.get(kind, set()):
        raise ValueError(f'{path}: unexpected or missing Decisions profile fields')
    if kind == 'deepseek':
        return
    if not isinstance(settings['question_name'], str) or not settings['question_name'].strip():
        raise ValueError(f'{path}: question_name must be a nonempty string')
    if DECISION_TYPES[kind] == 'choice':
        seen = set()
        for entry in _entries(settings, path, 'choices', 255, {'value', 'failed'}):
            value = entry['value']
            if type(value) not in (str, bool) or value in seen:
                raise ValueError(f'{path}: choices require unique string or boolean values')
            if type(entry['failed']) is not bool:
                raise ValueError(f'{path}: choices.failed must be boolean')
            seen.add(value)
        return
    maximum = 1
    if DECISION_TYPES[kind] == 'score':
        levels = _entries(settings, path, 'levels', 10, {'label'})
        if any(not isinstance(level['label'], str) or not level['label'].strip() for level in levels):
            raise ValueError(f'{path}: levels.label must be a nonempty string')
        maximum = len(levels) - 1
    rule = settings['fail_if']
    if (not isinstance(rule, dict) or rule.keys() != {'op', 'threshold'}
            or rule['op'] not in ('>=', '<=') or not _number_in_range(rule['threshold'], maximum)):
        raise ValueError(f'{path}: fail_if requires >= or <= and a threshold from 0 to {maximum}')


def decision_question(settings, instructions, descriptions):
    kind = DECISION_TYPES[settings['type']]
    question = {'type': kind, 'name': settings['question_name'], 'instructions': instructions}
    if kind in ('choice', 'score'):
        field, key = ('choices', 'value') if kind == 'choice' else ('levels', 'label')
        entries = []
        for index, entry in enumerate(settings[field]):
            option = {key: entry[key]}
            if descriptions[index] is not None:
                option['description'] = descriptions[index]
            entries.append(option)
        question[field] = entries
    return question


def decision_failed(settings, answer):
    kind = DECISION_TYPES[settings['type']]
    if answer.get('type') == 'refusal':
        raise ProviderError('GPT Decisions 拒绝本次检查')
    if answer.get('type') != kind or answer.get('name') != settings['question_name']:
        raise ProviderError(_CONTRACT_ERROR)
    if kind == 'choice':
        value = answer.get('choice')
        for entry in settings['choices']:
            if type(value) is type(entry['value']) and value == entry['value']:
                return entry['failed']
        raise ProviderError(_CONTRACT_ERROR)
    value = answer.get('probability' if kind == 'predicate' else 'score')
    maximum = 1 if kind == 'predicate' else len(settings['levels']) - 1
    if not _number_in_range(value, maximum):
        raise ProviderError(_CONTRACT_ERROR)
    rule = settings['fail_if']
    return value >= rule['threshold'] if rule['op'] == '>=' else value <= rule['threshold']
