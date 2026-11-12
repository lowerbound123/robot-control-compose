"""Freeze model-facing text alongside an llm_failure observation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from string import Formatter

from .prompts import VARIABLE_FIELD
from .request_size import MAX_REQUEST_BYTES, TextBudget


_VARIABLE = re.compile(r'\{' + VARIABLE_FIELD.pattern + r'\}')
_FEEDBACK = {'qpos': 'qpos', 'qvel': 'qvel', 'effort': 'effort', 'timestamp': 'timestamp',
             'eef': 'eef', 'epos': 'eef'}


def _as_text(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except Exception:
        # A single unencodable value must not discard its frame or its neighbours.
        return ''


class _InputTemplate:
    def __init__(self, source):
        self.parts = tuple((literal, field) for literal, field, _, _ in Formatter().parse(source))
        self.variables = {field[1:] for _, field in self.parts if field and field.startswith('$')}

    def freeze(self, variables, budget, **fields):
        # Only the window index is filled later. Values and escaped braces become
        # literal text, so even a substituted "{index}" cannot become a field.
        segments, literals = [], []
        for literal, field in self.parts:
            literals.append(budget.text(literal))
            if field == 'index':
                budget.reserve(1)  # Every eventual 1-based index has at least one digit.
                segments.append(''.join(literals))
                literals.clear()
            elif field is not None:
                value = variables.get(field[1:], '') if field.startswith('$') else str(fields[field])
                literals.append(budget.text(value))
        segments.append(''.join(literals))
        return tuple(segments)


@dataclass(frozen=True)
class FrameText:
    caption: tuple[str, ...]
    cameras: tuple[str, ...]


@dataclass(frozen=True)
class RequestText:
    task: str
    instructions: str
    descriptions: tuple[str | None, ...]


class PromptTemplates:
    def __init__(self, settings):
        templates = settings['input_templates']
        self.task_input = _InputTemplate(templates['task'])
        self.history = _InputTemplate(templates['history_snapshot'])
        self.current = _InputTemplate(templates['current_snapshot'])
        self.camera = _InputTemplate(templates['camera'])
        self.task, self.prompt, self.instructions = (settings[key] for key in ('task', 'prompt', 'response_instructions'))
        options = settings.get('choices', settings.get('levels', ()))
        self.descriptions = tuple(option.get('description') for option in options)
        self.history_names = self.history.variables | self.camera.variables
        texts = (self.task, self.prompt, self.instructions, *self.descriptions)
        self.all_names = self.history_names | self.current.variables | self.task_input.variables | {
            name for text in texts if text is not None for name in _VARIABLE.findall(text)}

    def sample_values(self, state, runtime, *, submitting):
        names = self.all_names if submitting else self.history_names
        shared = {}
        if names.difference(_FEEDBACK):
            try:
                _, shared = runtime.globals.snapshot()
                if not isinstance(shared, dict):
                    shared = {}
            except Exception:
                # An unavailable/detached shared store leaves just those variables blank.
                shared = {}
        return {name: _as_text(state.get(_FEEDBACK[name], '') if name in _FEEDBACK else shared.get(name, ''))
                for name in names}

    def frame_texts(self, values, step, cameras, *, include_current):
        budget = TextBudget(MAX_REQUEST_BYTES)
        camera_text = tuple(self.camera.freeze(values, budget, camera=camera)[0] for camera in cameras)
        # A frame is either current or historical in any single request.
        history = FrameText(self.history.freeze(values, TextBudget(budget.remaining), step=step), camera_text)
        current = FrameText(self.current.freeze(values, budget, step=step), camera_text) if include_current else None
        return history, current

    def request_text(self, values):
        def expand(source, budget):
            parts = []
            start = 0
            for match in _VARIABLE.finditer(source):
                parts.append(budget.text(source[start:match.start()]))
                parts.append(budget.text(values.get(match[1], '')))
                start = match.end()
            parts.append(budget.text(source[start:]))
            return ''.join(parts)

        budget = TextBudget(MAX_REQUEST_BYTES)
        task = expand(self.task, TextBudget(MAX_REQUEST_BYTES))
        task_input = self.task_input.freeze(values, budget, task=task)[0]
        prompt, suffix = expand(self.prompt, budget), expand(self.instructions, budget)
        instructions = prompt + budget.text('\n\n') + suffix if prompt and suffix else prompt or suffix
        descriptions = tuple(expand(text, budget) if text is not None else None for text in self.descriptions)
        return RequestText(task_input, instructions, descriptions)
