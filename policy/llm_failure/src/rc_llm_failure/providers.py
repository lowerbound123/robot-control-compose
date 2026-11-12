"""Single-request failure checks through DeepSeek Chat or GPT Decisions."""
import copy
import json
from time import monotonic, time

from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError, OpenAIError

from .decisions import DECISION_TYPES, decision_failed, decision_question
from .errors import LocalPolicyError, ProviderError
from .diagnostics import provider_metadata, sanitized_metadata
from .images import CheckInput
from .request_size import MAX_REQUEST_BYTES, TextBudget, check_body_size
from .prompt_variables import PromptTemplates
from .prompts import validate_prompt_fields


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def parse_decision(text):
    if not isinstance(text, str) or len(text) > 8192:
        raise ValueError('missing or oversized response')
    value = json.loads(text, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) != {'failed'} or type(value['failed']) is not bool:
        raise ValueError('failed must be the only field and a boolean')
    return value['failed']


class ProviderClient:
    def __init__(self, settings, *, diagnostics=None):
        self.settings = copy.deepcopy(settings)
        self.diagnostics = diagnostics
        self.kind = settings.get('type')
        key = settings.get('api_key')
        if not isinstance(key, str) or not key.strip():
            raise ValueError('provider.api_key is required')
        validate_prompt_fields(self.settings, 'provider')
        self.label = 'GPT Decisions' if self.kind in DECISION_TYPES else 'DeepSeek'
        self.prompts = PromptTemplates(self.settings)
        self.client = OpenAI(api_key=key.strip(), base_url=settings['base_url'],
                             timeout=settings['timeout_s'], max_retries=0)
        if self.kind in DECISION_TYPES and not hasattr(self.client, 'decisions'):
            self.client.close()
            raise ValueError('OpenAI SDK lacks Decisions; install the policy dependency lock')

    def decide(self, snapshots):
        metadata = provider_metadata(self.settings['model'], self.kind)
        try:
            text = snapshots.text if isinstance(snapshots, CheckInput) else self.prompts.request_text({})
            content = self._input_content(snapshots, text.task)
            failed = (self._decide_decisions(text.instructions, content, metadata, text.descriptions)
                      if self.kind in DECISION_TYPES else self._decide(text.instructions, content, metadata))
            metadata['parsed_failed'] = failed
            return failed
        except (LocalPolicyError, ProviderError) as exc:
            metadata['error'] = str(exc)
            raise
        except APITimeoutError:
            metadata['error'] = f'{self.label} API 请求超时；未自动重试'
            raise ProviderError(metadata['error']) from None
        except APIConnectionError:
            metadata['error'] = f'{self.label} API 连接失败；未自动重试'
            raise ProviderError(metadata['error']) from None
        except APIStatusError as exc:
            metadata['error'] = f'{self.label} API HTTP {int(exc.status_code)}；未自动重试'
            raise ProviderError(metadata['error']) from None
        except OpenAIError:
            metadata['error'] = f'{self.label} 响应不符合失败判断契约'
            raise ProviderError(metadata['error']) from None
        except Exception:
            metadata['error'] = f'{self.label} 检查异常'
            raise
        finally:
            if self.diagnostics is not None:
                try:
                    key = self.settings['api_key']
                    self.diagnostics(sanitized_metadata(metadata, (key, key.strip())))
                except Exception:
                    pass  # Optional instrumentation cannot change the decision.

    def _capture_http(self, metadata, response, request_id=None):
        try:
            metadata['http_status'] = response.status_code
            metadata['provider_request_id'] = request_id or response.headers.get('x-request-id')
            if self.diagnostics is not None:
                metadata['raw_response'] = response.text
        except Exception:
            pass

    def _capture_completion(self, metadata, response):
        try:
            metadata['response_model'] = response.model
            metadata['provider_request_id'] = getattr(response, '_request_id', None) or metadata['provider_request_id']
            usage = response.usage
            if usage is not None:
                metadata['usage'] = {name: value for name in ('prompt_tokens', 'completion_tokens', 'total_tokens',
                                                             'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
                                     if type(value := getattr(usage, name, None)) is int}
            if response.choices:
                metadata['finish_reason'] = response.choices[0].finish_reason
        except Exception:
            pass

    def _input_content(self, snapshots, task):
        decisions = self.kind in DECISION_TYPES
        text_type = 'input_text' if decisions else 'text'
        budget = TextBudget(MAX_REQUEST_BYTES, self.label)
        content = [{'type': text_type, 'text': budget.text(task)}]
        for index, sample in enumerate(snapshots, 1):
            captions = sample.captions
            if captions is None:  # Direct decide() calls can pass plain JPEG snapshots.
                latest = index == len(snapshots)
                history, current = self.prompts.frame_texts({}, sample.step, (name for name, _ in sample.images),
                                                           include_current=latest)
                captions = current if latest else history
            parts = captions.caption
            for part in parts:
                budget.text(part)
            budget.reserve((len(parts) - 1) * len(str(index)))
            content.append({'type': text_type, 'text': str(index).join(parts)})
            for caption, (_, data) in zip(captions.cameras, sample.images, strict=True):
                budget.reserve(len(data))
                content.append({'type': text_type, 'text': budget.text(caption)})
                content.append({'type': 'input_image', 'image_url': data} if decisions else
                               {'type': 'image_url', 'image_url': {'url': data}})
        return content

    def _decide(self, instructions, content, metadata):
        body = {'model': self.settings['model'],
                'messages': [{'role': 'system', 'content': instructions},
                             {'role': 'user', 'content': content}],
                'response_format': {'type': 'json_object'}, 'temperature': 0, 'max_tokens': 128,
                'thinking': {'type': 'disabled'}}
        check_body_size(body, MAX_REQUEST_BYTES, self.label)
        metadata['http_started_wall_s'], metadata['http_started_mono_s'] = time(), monotonic()
        try:
            try:
                raw = self.client.chat.completions.with_raw_response.create(
                    model=body['model'], messages=body['messages'], response_format=body['response_format'],
                    temperature=0, max_tokens=128, extra_body={'thinking': body['thinking']})
            finally:
                metadata['http_finished_mono_s'], metadata['http_finished_wall_s'] = monotonic(), time()
                metadata['http_duration_s'] = metadata['http_finished_mono_s'] - metadata['http_started_mono_s']
        except APIStatusError as exc:
            self._capture_http(metadata, exc.response, exc.request_id)
            raise
        self._capture_http(metadata, raw)
        response = self._parse_response(raw)
        self._capture_completion(metadata, response)
        choices = getattr(response, 'choices', None)
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProviderError(f'{self.label} 响应不符合失败判断契约')
        choice = choices[0]
        message = getattr(choice, 'message', None)
        if getattr(message, 'refusal', None):
            raise ProviderError(f'{self.label} 拒绝本次检查')
        if getattr(choice, 'finish_reason', None) != 'stop':
            raise ProviderError(f'{self.label} 未返回完整判断')
        try:
            return parse_decision(getattr(message, 'content', None))
        except (ValueError, RecursionError):
            raise ProviderError(f'{self.label} 响应不符合失败判断契约') from None

    def _decide_decisions(self, instructions, content, metadata, descriptions):
        body = {'model': self.settings['model'],
                'input': [{'role': 'user', 'content': content}],
                'questions': [decision_question(self.settings, instructions, descriptions)]}
        check_body_size(body, MAX_REQUEST_BYTES, self.label)
        metadata['http_started_wall_s'], metadata['http_started_mono_s'] = time(), monotonic()
        try:
            try:
                raw = self.client.decisions.with_raw_response.create(**body)
            finally:
                metadata['http_finished_mono_s'], metadata['http_finished_wall_s'] = monotonic(), time()
                metadata['http_duration_s'] = metadata['http_finished_mono_s'] - metadata['http_started_mono_s']
        except APIStatusError as exc:
            self._capture_http(metadata, exc.response, exc.request_id)
            raise
        self._capture_http(metadata, raw)
        # The SDK's model parser coerces JSON booleans to float for predicate
        # and score. Check the actual wire types before applying a failure rule.
        def reject_constant(_):
            raise ValueError('nonstandard JSON number')

        try:
            response = json.loads(raw.content, object_pairs_hook=_unique_object, parse_constant=reject_constant)
        except (ValueError, RecursionError):
            raise ProviderError('GPT Decisions 响应不符合失败判断契约') from None
        if not isinstance(response, dict):
            raise ProviderError('GPT Decisions 响应不符合失败判断契约')
        metadata['response_model'] = response.get('model')
        usage = response.get('usage')
        if isinstance(usage, dict):
            metadata['usage'] = {name: value for name in ('input_tokens', 'output_tokens', 'total_tokens')
                                 if type(value := usage.get(name)) is int}
            for name, fields in (('input_tokens_details', ('cached_tokens', 'cache_write_tokens')),
                                 ('output_tokens_details', ('reasoning_tokens',))):
                details = usage.get(name)
                if isinstance(details, dict):
                    metadata['usage'][name] = {field: value for field in fields
                                              if type(value := details.get(field)) is int}
        answers = response.get('answers')
        if not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict):
            raise ProviderError('GPT Decisions 响应不符合失败判断契约')
        answer = answers[0]
        metadata['finish_reason'] = answer.get('type')
        return decision_failed(self.settings, answer)

    def _parse_response(self, raw):
        # Only decoding remote bytes turns parse errors into recoverable failures.
        # Request construction and programming errors retain their fault semantics.
        try:
            return raw.parse()
        except (ValueError, RecursionError, OpenAIError):
            raise ProviderError(f'{self.label} 响应不符合失败判断契约') from None

    def close(self):
        self.client.close()
