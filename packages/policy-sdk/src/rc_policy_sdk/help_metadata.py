"""Bounded plain-text help metadata; standard library only, no evaluation."""
import json


def help_string(value, field='help', maximum=4096):
    if (type(value) is not str or len(value) > maximum or
            any((ord(char) < 32 and char not in '\n\t') or 127 <= ord(char) < 160 for char in value)):
        raise ValueError(f'{field} must be bounded plain text')
    return value


def normalize_help_details(raw):
    """Validate optional details when present; missing fields get no invented text."""
    if type(raw) is not dict or set(raw) - {'group', 'description', 'sections', 'examples'}:
        raise ValueError('help_details must contain only group/description/sections/examples')
    result = {}
    for field, maximum in (('group', 128), ('description', 16384)):
        if field in raw:
            result[field] = help_string(raw[field], 'help_details.' + field, maximum)
    for field, keys, limit in (('sections', ('title', 'body'), 32), ('examples', ('title', 'command'), 64)):
        if field not in raw:
            continue
        entries = raw[field]
        if type(entries) is not list or len(entries) > limit:
            raise ValueError(f'help_details.{field} must be a bounded list')
        result[field] = []
        for entry in entries:
            if type(entry) is not dict or set(entry) != set(keys):
                raise ValueError(f'help_details.{field} entries require {"/".join(keys)}')
            result[field].append({key: help_string(entry[key], f'help_details.{field}.{key}',
                256 if key == 'title' else 4096 if key == 'command' else 16384) for key in keys})
    if len(json.dumps(result, ensure_ascii=False)) > 131072:
        raise ValueError('help_details exceeds the total size limit')
    return result
