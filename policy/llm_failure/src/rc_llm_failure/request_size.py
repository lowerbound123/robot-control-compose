"""Bound failure-check text before joining it or serializing a history window."""
import json
from json.encoder import encode_basestring_ascii

from .errors import LocalPolicyError


MAX_REQUEST_BYTES = 32 * 1024 * 1024


class TextBudget:
    def __init__(self, remaining, label='llm_failure'):
        self.remaining = remaining
        self.label = label

    def reserve(self, size):
        if size > self.remaining:
            raise LocalPolicyError(f'{self.label} 请求超过32MiB，请减小提示词、历史数量、图像尺寸或JPEG质量')
        self.remaining -= size

    def text(self, value):
        # Match ensure_ascii=True without allocating a large escaped copy just to measure it.
        for start in range(0, len(value), 16384):
            self.reserve(len(encode_basestring_ascii(value[start:start + 16384])) - 2)
        return value


def check_body_size(body, maximum, label):
    budget = TextBudget(maximum, label)
    for part in json.JSONEncoder(ensure_ascii=True, allow_nan=False).iterencode(body):
        budget.reserve(len(part))
