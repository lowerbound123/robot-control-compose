"""Copies for public output; actual configuration remains intact for workers/save."""
import ast
from bisect import bisect_left
import copy
import json
import re

_LITERAL_START = re.compile(r"(?:[rRuUbB]|[bB][rR]|[rR][bB])?(?=['\"])")


def public_value(value):
    if isinstance(value, dict):
        sensitive_patch = 'api_key' in str(value.get('path', '')).split('.')
        return {key: '***' if key == 'api_key' or (key == 'value' and sensitive_patch)
                else public_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_value(item) for item in value]
    if isinstance(value, str):
        return public_message(value)
    return copy.deepcopy(value)


def public_message(value):
    return _redact_message(value, scan_comments=True)


def _redact_message(value, *, scan_comments):
    if not isinstance(value, str):
        return json.dumps(public_value(value), ensure_ascii=False)
    if '{' not in value and '[' not in value:
        return value
    tokens, comments = _message_tokens(value, scan_comments=scan_comments)
    stack, scopes, closing = [], {}, {}
    for index, (kind, start, end, decoded) in enumerate(tokens):
        scopes[index] = stack[-1] if stack else None
        if kind in ('{', '[', '('):
            stack.append(index)
        elif kind in ('}', ']', ')') and stack and tokens[stack[-1]][0] == {'}':'{', ']':'[', ')':'('}[kind]:
            closing[stack.pop()] = index

    opening = {end: start for start, end in closing.items()}
    # Compute complete same-scope value boundaries once. A literal may span
    # multiple tokens (e.g. 123 + 456j), including parenthesized components.
    next_boundary, value_ends = {}, {}
    for index in range(len(tokens) - 1, -1, -1):
        if tokens[index][0] in (',', '}', ']', ')'):
            next_boundary[scopes[index]] = index
        boundary = next_boundary.get(scopes[index])
        value_ends[index] = (len(value) if boundary is None else tokens[boundary - 1][2]
                             if boundary > index else tokens[index][1])

    def value_span(index):
        return tokens[index][1], value_ends[index]

    # A constant string can be wrapped in any number of matched parentheses.
    # Cache inner-to-outer spans instead of walking them again at every colon.
    string_spans = {}
    for index, (kind, start, end, decoded) in enumerate(tokens):
        if kind == 'string':
            string_spans[index] = (index, index)
        elif kind == ')' and index in opening:
            lower = opening[index]
            inner = string_spans.get(lower + 1)
            if inner is not None and inner[0] == index - 1:
                string_spans[lower] = (index, inner[1])

    def string_literal(start, end):
        span = string_spans.get(start)
        return span[1] if span is not None and span[0] == end else None

    replacements, sensitive_scopes, values, literal_keys = [], set(), [], set()
    for index, (kind, start, end, decoded) in enumerate(tokens):
        if kind != ':' or index == 0 or index + 1 >= len(tokens):
            continue
        key_start = opening.get(index - 1, index - 1)
        key_token = string_literal(key_start, index - 1)
        decoded = tokens[key_token][3] if key_token is not None else None
        if key_token is not None:
            literal_keys.add(key_token)
        scope = scopes[key_start]
        if decoded == 'api_key':
            replacements.append((*value_span(index + 1), '"***"'))
        if scope is not None and tokens[scope][0] == '{':
            if decoded == 'path':
                path_token = string_literal(index + 1, closing.get(index + 1, index + 1))
                path = tokens[path_token][3] if path_token is not None else None
                if isinstance(path, str) and 'api_key' in path.split('.'):
                    sensitive_scopes.add(scope)
            elif decoded == 'value':
                values.append((scope, *value_span(index + 1)))
    replacements.extend((start, end, '"***"') for scope, start, end in values if scope in sensitive_scopes)
    # Mask outer values first; nested sensitive keys cannot trigger repeated
    # scans of their parent object. Both valid and invalid enclosing text work.
    masks = _disjoint_replacements(replacements)
    mask_index = 0
    for index, (kind, start, end, decoded) in enumerate(tokens):
        while mask_index < len(masks) and masks[mask_index][1] <= start:
            mask_index += 1
        if mask_index < len(masks) and masks[mask_index][0] <= start < masks[mask_index][1]:
            continue
        if kind in ('string', 'invalid_string') and isinstance(decoded, str):
            if index in literal_keys:
                # public_value leaves dictionary keys intact, but the old log
                # parser also saw valid JSON fragments in their ORIGINAL text.
                # Preserve that visibility without decoding an encoded JSON key
                # into a new sensitive value or excluding an enclosing range.
                original = value[start:end]
                clean = public_message(original)
                if clean != original:
                    replacements.append((start, end, clean))
            else:
                clean = public_message(decoded)
                if clean != decoded:
                    encoded = (value[start] + clean + value[start] if kind == 'invalid_string' else
                               json.dumps(clean, ensure_ascii=False) if value[start] == '"' else repr(clean))
                    replacements.append((start, end, encoded))
    # Python comments may themselves contain independently embedded JSON in a
    # malformed outer log fragment. Scan their disjoint text once; disabling
    # comment recognition prevents nested '#' text from rescanning suffixes.
    for start, end in comments:
        original = value[start:end]
        clean = _redact_message(original, scan_comments=False)
        if clean != original:
            replacements.append((start, end, clean))
    if replacements:
        parts, cursor = [], 0
        for start, end, replacement in _disjoint_replacements(replacements):
            parts.extend((value[cursor:start], replacement))
            cursor = end
        parts.append(value[cursor:])
        value = ''.join(parts)
    # Also hide a recognizable key in malformed JSON without echoing its value.
    return re.sub(r'''((?:"api_key"|'api_key')\s*:\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,}\]\n]*)''',
                  r'\1"***"', value)


def _disjoint_replacements(replacements):
    result = []
    for span in sorted(replacements, key=lambda span: (span[0], -span[1])):
        if not result or span[0] >= result[-1][1]:
            result.append(span)
    return result


def _message_tokens(text, *, scan_comments):
    """One lexical pass, with quote lookahead built once instead of rescanning.

    Brackets are never parsed recursively. JSON-encoded string values are
    checked at their decoded string layer by public_message, like public_value.
    """
    quotes = {quote: [] for quote in ('"', "'", '"""', "'''")}
    escaped = False
    for index, char in enumerate(text):
        if char in ('"', "'") and not escaped:
            quotes[char].append(index)
            if text.startswith(char * 3, index):
                quotes[char * 3].append(index)
        escaped = not escaped if char == '\\' else False
    tokens, containers, comments, index = [], [], [], 0
    separators = '{}[]():,\"\''
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if containers and (text.startswith('\\\n', index) or text.startswith('\\\r\n', index)):
            index += 3 if text.startswith('\\\r\n', index) else 2
            continue
        if char == '#' and containers and scan_comments:
            newline = text.find('\n', index)
            end = len(text) if newline < 0 else newline
            comments.append((index + 1, end))
            index = end
            continue
        # Plain log prefixes are not Python string literals. In particular an
        # apostrophe in "can't" must not consume a later JSON key's quote.
        eligible = containers and tokens[-1][0] in ('{', '[', '(', ':', ',', 'string', 'bytes')
        literal = _LITERAL_START.match(text, index) if eligible else None
        quote_start = literal.end() if literal else index
        delimiter = text[quote_start:quote_start + 1]
        if literal and text.startswith(delimiter * 3, quote_start):
            delimiter *= 3
        candidates = quotes.get(delimiter, ())
        position = bisect_left(candidates, quote_start + len(delimiter)) if literal else len(candidates)
        if literal and position < len(candidates):
            end = candidates[position] + len(delimiter)
            token = text[index:end]
            try:
                decoded = (json.loads(token) if quote_start == index and delimiter == '"'
                           else ast.literal_eval(token))
            except (ValueError, SyntaxError, RecursionError):
                try: decoded = ast.literal_eval(token)
                except (ValueError, SyntaxError, RecursionError): decoded = None
            kind = 'string' if isinstance(decoded, str) else 'bytes' if isinstance(decoded, bytes) else 'invalid_string'
            decoded = decoded if decoded is not None else text[quote_start + len(delimiter):end - len(delimiter)]
            if (kind in ('string', 'bytes') and tokens and tokens[-1][0] == kind
                    and not re.sub(r'\\\r?\n|#[^\n]*', '', text[tokens[-1][2]:index]).strip()):
                previous = tokens[-1]
                pieces = previous[3] if isinstance(previous[3], list) else [previous[3]]
                pieces.append(decoded)
                tokens[-1] = (kind, previous[1], end, pieces)
            else:
                tokens.append((kind, index, end, decoded))
            index = end
        elif char in separators:
            tokens.append((char, index, index + 1, None))
            if char in '{[(':
                containers.append(char)
            elif char in '}])' and containers and containers[-1] == {'}':'{', ']':'[', ')':'('}[char]:
                containers.pop()
            index += 1
        else:
            end = index + 1
            while end < len(text) and text[end] not in separators and not text[end].isspace():
                end += 1
            tokens.append(('bare', index, end, None))
            index = end
    return ([(kind, start, end, (b'' if kind == 'bytes' else '').join(decoded)
             if isinstance(decoded, list) else decoded) for kind, start, end, decoded in tokens], comments)


def _contains_key(value):
    if isinstance(value, dict):
        return 'api_key' in value or any(_contains_key(item) for item in value.values())
    return isinstance(value, list) and any(_contains_key(item) for item in value)


def reject_secret_edit(line):
    if not isinstance(line, str):
        return
    # Inspect supported envelopes before parsing/logging their content. Legacy
    # private execution lines retain the same guard during controller dispatch.
    parts = line.strip().split(maxsplit=2)
    if len(parts) >= 2 and parts[0] == 'mcp' and parts[1] in ('command', 'policy_config', 'tui_command'):
        raw = parts[2] if len(parts) == 3 else ''
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            if re.search(r'api(?:_|\\u005[fF])key', raw):
                raise ValueError('密钥只能在配置文件中修改') from None
            return
        if not isinstance(data, dict):
            return
        if parts[1] == 'command':
            return reject_secret_edit(data.get('line', ''))
        if parts[1] == 'tui_command':
            batch = data.get('text', '')
            if isinstance(batch, str):
                for command in batch.splitlines():
                    reject_secret_edit(command)
            return
        if data.get('action') == 'set':
            return _reject_secret_value(data.get('key', ''), data.get('value_text', ''))
        return
    tokens = line.strip().split(maxsplit=5)
    if len(tokens) >= 4 and tokens[0] == 'policy' and tokens[2:4] == ['config', 'set']:
        return _reject_secret_value(tokens[4] if len(tokens) > 4 else '', tokens[5] if len(tokens) > 5 else '')
    tokens = line.strip().split(maxsplit=4)
    if tokens[:3] == ['config', 'rc', 'set']:
        return _reject_secret_value(tokens[3] if len(tokens) > 3 else '', tokens[4] if len(tokens) > 4 else '')
    tokens = line.strip().split(maxsplit=3)
    if tokens[:2] == ['global', 'set']:
        return _reject_secret_value(tokens[2] if len(tokens) > 2 else '', tokens[3] if len(tokens) > 3 else '')
    if parts and parts[0] == 'set' and len(parts) >= 2:
        return _reject_secret_value(parts[1], parts[2] if len(parts) > 2 else '')


def _reject_secret_value(key, text):
    if isinstance(key, str) and 'api_key' in key.split('.'):
        raise ValueError('密钥只能在配置文件中修改')
    if not isinstance(text, str):
        return
    try:
        value = json.loads(text)
    except ValueError:
        if re.search(r'api(?:_|\\u005[fF])key', text):
            raise ValueError('密钥只能在配置文件中修改') from None
        return
    if _contains_key(value):
        raise ValueError('密钥只能在配置文件中修改')
