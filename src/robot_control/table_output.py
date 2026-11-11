"""RC publication boundary for table projections and their original JSON copy."""
import json
from collections import deque

from rc_policy_sdk.tables import normalize_table_document, table_document_text
from .secret_config import public_value


def public_json_fields(result):
    text = json.dumps(public_value(result), ensure_ascii=False, allow_nan=False)
    return {'message': text, 'copy_json': text}


def public_table_document(document):
    document = public_value(normalize_table_document(document))
    for table in document['tables']:
        columns = table['columns']
        for row in table['rows']:
            # Preserve the existing api_key and path/value semantics even when
            # object fields were projected into columns or key/value rows.
            sensitive_value = any(
                column in ('path', 'key', 'field', 'fields', 'name', '项目', '字段')
                and isinstance(cell, str) and 'api_key' in cell.split('.')
                for column, cell in zip(columns, row))
            for index, column in enumerate(columns):
                if ('api_key' in column.split('.')
                        or (sensitive_value and column in ('value', 'value_text', '值'))):
                    row[index] = '***'
    return document


def _hidden_strings(original, public):
    """Use existing redaction decisions, without classifying new credentials."""
    if public == '***' and original != '***':
        if isinstance(original, str):
            return {original} if original else set()
        children = original.values() if isinstance(original, dict) else original if isinstance(original, list) else ()
        return set().union(*(_hidden_strings(child, '***') for child in children))
    if isinstance(original, dict) and isinstance(public, dict):
        return set().union(*(_hidden_strings(value, public[key]) for key, value in original.items()))
    if isinstance(original, list) and isinstance(public, list):
        return set().union(*(_hidden_strings(value, clean) for value, clean in zip(original, public)))
    return set()


class _SecretMatcher:
    """Reusable Aho-Corasick matcher; scan each text once, independent of key count.

    At each state only the longest ending match is needed: its span covers all
    shorter matches at that position. Overlapping sensitive spans form a union.
    """
    def __init__(self, secrets):
        self.edges, self.failure, self.length = [{}], [0], [0]
        for secret in secrets:
            state = 0
            for char in secret:
                child = self.edges[state].get(char)
                if child is None:
                    child = len(self.edges)
                    self.edges[state][char] = child
                    self.edges.append({})
                    self.failure.append(0)
                    self.length.append(0)
                state = child
            self.length[state] = max(self.length[state], len(secret))
        pending = deque(self.edges[0].values())
        while pending:
            state = pending.popleft()
            for char, child in self.edges[state].items():
                fallback = self.failure[state]
                while fallback and char not in self.edges[fallback]:
                    fallback = self.failure[fallback]
                self.failure[child] = self.edges[fallback].get(char, 0)
                self.length[child] = max(self.length[child], self.length[self.failure[child]])
                pending.append(child)

    def hide(self, text):
        state, spans = 0, []
        for end, char in enumerate(text, 1):
            while state and char not in self.edges[state]:
                state = self.failure[state]
            state = self.edges[state].get(char, 0)
            length = self.length[state]
            if length:
                start = end - length
                while spans and spans[-1][1] >= start:
                    start = min(start, spans.pop()[0])
                spans.append((start, end))
        if not spans:
            return text
        parts, start = [], 0
        for lower, upper in spans:
            parts.extend((text[start:lower], '***'))
            start = upper
        parts.append(text[start:])
        return ''.join(parts)


def _hide_strings(value, matcher):
    if isinstance(value, str):
        return matcher.hide(value)
    if isinstance(value, dict):
        return {_hide_strings(key, matcher): _hide_strings(item, matcher) for key, item in value.items()}
    if isinstance(value, list):
        return [_hide_strings(item, matcher) for item in value]
    return value


def public_table_fields(result, document):
    """Return independent public fields; never mutate business data/config."""
    public = public_value(result)
    document = public_table_document(document)
    # An SDK caller may choose arbitrary/localized headings. Carry redaction
    # of known hidden business strings into every textual table projection.
    secrets = _hidden_strings(result, public)
    if secrets:
        matcher = _SecretMatcher(secrets)
        for table in document['tables']:
            for key in ('title', 'columns', 'rows'):
                table[key] = _hide_strings(table[key], matcher)
    return {'table_document': document, 'message': table_document_text(document),
            'copy_json': json.dumps(public, ensure_ascii=False, allow_nan=False)}
