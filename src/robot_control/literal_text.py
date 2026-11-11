"""Literal terminal text and linear source-boundary mapping, without UI imports."""
import re

_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')


def clean_text(value: str) -> str:
    """Preserve real newlines, expand tabs and render terminal controls literally."""
    return _CONTROL.sub('�', value.replace('\r\n', '\n').replace('\r', '\n')).expandtabs(4)


def clean_boundaries(value: str, boundaries):
    """Return clean_text(value) and offsets for requested raw character boundaries.

    CRLF may straddle cells/separators. The LF owns the normalized newline;
    a preceding CR contributes no character. Tabs use the full logical line's
    character column, matching str.expandtabs rather than terminal cell width.
    Each character and requested boundary is visited once.
    """
    wanted = set(boundaries)
    offsets, parts, length, column = {}, [], 0, 0
    for index, char in enumerate(value):
        if index in wanted:
            offsets[index] = length
        if char == '\r' and index + 1 < len(value) and value[index + 1] == '\n':
            continue
        if char in '\r\n':
            piece, column = '\n', 0
        elif char == '\t':
            piece = ' ' * (4 - column % 4)
            column += len(piece)
        else:
            piece = '�' if _CONTROL.fullmatch(char) else char
            column += 1
        parts.append(piece)
        length += len(piece)
    if len(value) in wanted:
        offsets[len(value)] = length
    return ''.join(parts), offsets
