"""Serializable semantic help and its lossless plain-text projection.

No Rich/Textual imports: log files, MCP and terminals share this representation.
Renderers may style characters, but HelpLine text is the canonical copy content.
"""
from dataclasses import dataclass
import json
from typing import Any, TypedDict


class HelpSection(TypedDict):
    title: str
    body: str


class HelpCommand(TypedDict):
    group: str
    command: str
    summary: str


class HelpParameter(TypedDict, total=False):
    name: str
    type: str
    required: bool
    description: str
    default: Any
    choices: list


class HelpExample(TypedDict):
    title: str
    command: str


class HelpDocument(TypedDict):
    schema_version: int
    title: str
    summary: str
    usage: list[str]
    sections: list[HelpSection]
    commands: list[HelpCommand]
    parameters: list[HelpParameter]
    examples: list[HelpExample]


@dataclass(frozen=True)
class HelpLine:
    text: str
    role: str


def document(title, summary='', **fields) -> HelpDocument:
    return {'schema_version': 1, 'title': title, 'summary': summary,
            **{name: fields.get(name, []) for name in ('usage', 'sections', 'commands', 'parameters', 'examples')}}


def parameter_text(parameter):
    flags = [parameter['type'], '必填' if parameter['required'] else '可选']
    if 'default' in parameter:
        flags.append('默认 ' + json.dumps(parameter['default'], ensure_ascii=False, allow_nan=False))
    if 'choices' in parameter:
        flags.append('候选 ' + json.dumps(parameter['choices'], ensure_ascii=False, allow_nan=False))
    text = f"{parameter['name']}  {' · '.join(flags)}"
    if parameter.get('description'):
        text += '\n' + parameter['description']
    return text


def help_lines(value: HelpDocument) -> list[HelpLine]:
    lines = []

    def add(text, role):
        lines.extend(HelpLine(line, role) for line in str(text).split('\n'))

    def heading(title):
        if lines and lines[-1].text:
            add('', 'blank')
        add(title, 'section')

    add(value['title'], 'title')
    if value.get('summary'):
        add(value['summary'], 'summary')
    if value.get('usage'):
        heading('用法')
        for usage in value['usage']:
            add(usage, 'usage')
    groups = {}
    for command in value.get('commands', []):
        groups.setdefault(command['group'], []).append(command)
    for group, commands in groups.items():
        heading(group or '命令')
        for command in commands:
            add(command['command'] + (' — ' + command['summary'] if command['summary'] else ''), 'catalog')
    for section in value.get('sections', []):
        heading(section['title'])
        add(section['body'], 'body')
    if value.get('parameters'):
        heading('参数')
        for parameter in value['parameters']:
            add(parameter_text(parameter), 'parameter')
    if value.get('examples'):
        heading('示例')
        for example in value['examples']:
            if example['title']:
                add(example['title'], 'body')
            add(example['command'], 'example')
    return lines


def help_text(value: HelpDocument) -> str:
    return '\n'.join(line.text for line in help_lines(value))


def help_payload(value: HelpDocument, **data):
    return {**data, 'help': help_text(value), 'help_document': value}
