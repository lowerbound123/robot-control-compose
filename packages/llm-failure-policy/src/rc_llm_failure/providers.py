"""Single-request failure checks through DeepSeek Chat or GPT Decisions."""
import copy
import json
from time import monotonic, time

from openai import OpenAI, APIConnectionError, APIStatusError, APITimeoutError, OpenAIError

from .errors import LocalPolicyError, ProviderError
from .diagnostics import provider_metadata, sanitized_metadata
from .images import MAX_REQUEST_BYTES
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
        self.kind = settings.get('type', 'deepseek')
        self.label = 'GPT Decisions' if self.kind == 'openai_decisions' else 'DeepSeek'
        key = settings.get('api_key')
        if not isinstance(key, str) or not key.strip():
            raise ValueError('provider.api_key is required')
        validate_prompt_fields(self.settings, 'provider')
        self.client = OpenAI(api_key=key.strip(), base_url=settings['base_url'],
                             timeout=settings['timeout_s'], max_retries=0)
        if self.kind == 'openai_decisions' and not hasattr(self.client, 'decisions'):
            self.client.close()
            raise ValueError('OpenAI SDK lacks Decisions; install the policy dependency lock')

    def decide(self, snapshots):
        metadata = provider_metadata(self.settings['model'], self.kind)
        try:
            instructions = '\n\n'.join(part for part in (self.settings['prompt'], self.settings['response_instructions']) if part)
            content = self._input_content(snapshots)
            failed = (self._decide_decisions(instructions, content, metadata)
                      if self.kind == 'openai_decisions' else self._decide(instructions, content, metadata))
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

    def _input_content(self, snapshots):
        decisions = self.kind == 'openai_decisions'
        text_type = 'input_text' if decisions else 'text'
        templates = self.settings['input_templates']
        content = [{'type': text_type, 'text': templates['task'].format(task=self.settings['task'])}]
        for index, sample in enumerate(snapshots, 1):
            template = templates['current_snapshot' if index == len(snapshots) else 'history_snapshot']
            content.append({'type': text_type, 'text': template.format(index=index, step=sample.step)})
            for camera, data in sample.images:
                content.append({'type': text_type, 'text': templates['camera'].format(camera=camera)})
                content.append({'type': 'input_image', 'image_url': data} if decisions else
                               {'type': 'image_url', 'image_url': {'url': data}})
        return content

    def _decide(self, instructions, content, metadata):
        body = {'model': self.settings['model'],
                'messages': [{'role': 'system', 'content': instructions},
                             {'role': 'user', 'content': content}],
                'response_format': {'type': 'json_object'}, 'temperature': 0, 'max_tokens': 128,
                'thinking': {'type': 'disabled'}}
        if len(json.dumps(body, ensure_ascii=True, allow_nan=False).encode('utf-8')) > MAX_REQUEST_BYTES:
            raise LocalPolicyError(f'{self.label} 请求超过32MiB；请减小历史数量、图像尺寸或JPEG质量')
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

    def _decide_decisions(self, instructions, content, metadata):
        body = {'model': self.settings['model'],
                'input': [{'role': 'user', 'content': content}],
                'questions': [{'type': 'choice', 'name': 'failed',
                               'instructions': instructions,
                               'choices': [{'value': value, 'description': self.settings['choice_descriptions'][value]}
                                           for value in ('not_failed', 'failed')]}]}
        if len(json.dumps(body, ensure_ascii=True, allow_nan=False).encode('utf-8')) > MAX_REQUEST_BYTES:
            raise LocalPolicyError('GPT Decisions 请求超过32MiB；请减小历史数量、图像尺寸或JPEG质量')
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
        response = self._parse_response(raw)
        metadata['response_model'] = getattr(response, 'model', None)
        metadata['provider_request_id'] = getattr(response, '_request_id', None) or metadata['provider_request_id']
        usage = getattr(response, 'usage', None)
        if usage is not None:
            metadata['usage'] = {name: value for name in ('input_tokens', 'output_tokens', 'total_tokens')
                                 if type(value := getattr(usage, name, None)) is int}
            for name, fields in (('input_tokens_details', ('cached_tokens', 'cache_write_tokens')),
                                 ('output_tokens_details', ('reasoning_tokens',))):
                details = getattr(usage, name, None)
                if details is not None:
                    metadata['usage'][name] = {field: value for field in fields
                                              if type(value := getattr(details, field, None)) is int}
        answers = getattr(response, 'answers', None)
        if not isinstance(answers, list) or len(answers) != 1:
            raise ProviderError('GPT Decisions 响应不符合失败判断契约')
        answer = answers[0]
        metadata['finish_reason'] = getattr(answer, 'type', None)
        if metadata['finish_reason'] == 'refusal':
            raise ProviderError('GPT Decisions 拒绝本次检查')
        if metadata['finish_reason'] != 'choice' or getattr(answer, 'name', None) != 'failed':
            raise ProviderError('GPT Decisions 响应不符合失败判断契约')
        choice = getattr(answer, 'choice', None)
        if type(choice) is not str or choice not in ('failed', 'not_failed'):
            raise ProviderError('GPT Decisions 响应不符合失败判断契约')
        return answer.choice == 'failed'

    def _parse_response(self, raw):
        # Only decoding remote bytes turns parse errors into recoverable failures.
        # Request construction and programming errors retain their fault semantics.
        try:
            return raw.parse()
        except (ValueError, RecursionError, OpenAIError):
            raise ProviderError(f'{self.label} 响应不符合失败判断契约') from None

    def close(self):
        self.client.close()
