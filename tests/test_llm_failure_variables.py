"""Prompts are bound to the same observation as their owned JPEGs."""
import threading
import weakref

import pytest

from llm_failure_helpers import Provider, Runtime, done, images, manifest, provider_settings, wait
from llm_http_helpers import model_server, response_for
from rc_llm_failure.runtime import FailurePolicy


class Shared:
    def __init__(self):
        self.values = {}
        self.reads = 0
        self.fail = False

    def snapshot(self):
        self.reads += 1
        if self.fail:
            raise RuntimeError('unavailable')
        return self.reads, self.values.copy()


@pytest.mark.parametrize('kind', ['deepseek', 'gpt-decision-choice', 'gpt-decision-predicate', 'gpt-decision-score'])
def test_sampled_variables_reach_the_wire_without_reinterpretation(kind):
    pytest.importorskip('openai')
    profile = 'provider.yaml.example' if kind == 'deepseek' else f'providers/{kind}.yaml.example'
    chunk = [[0.] * 14]
    runtime = Runtime()
    runtime.globals = shared = Shared()
    with model_server() as server:
        settings = provider_settings(profile=profile, base_url=server.url, task='取{$phase}',
            prompt='v={$info};time={$time};missing={$missing};bad={$bad};nan={$nan};none={$none};'
                   'flag={$flag};pos={$epos}/{$eef};vel={$qvel};eff={$effort};stamp={$timestamp};JSON={"a":1}',
            response_instructions='本轮={$phase}',
            input_templates={'task': '{task} / {$phase}', 'history_snapshot': 'H{index}:{step}:{$qpos}:{$phase}',
                             'current_snapshot': 'C{index}:{step}:{$qpos}:{$phase}',
                             'camera': '{camera}={$time}'})
        answer = {'type': kind.removeprefix('gpt-decision-'), 'name': settings.get('question_name')}
        if kind.endswith('choice'):
            settings['choices'][0]['description'] = '选项 {$phase}'
            answer['choice'] = settings['choices'][0]['value']
        if kind.endswith('score'):
            settings['levels'][0]['description'] = '等级 {$phase}'
            answer['score'] = 0.25
        if kind.endswith('predicate'):
            answer['probability'] = 0.25
        reply = response_for('deepseek', {'failed': False}) if kind == 'deepseek' else {'answers': [answer]}
        server.responses.extend([{'body': reply}, {'body': reply}])
        policy = FailurePolicy(manifest(history_size=2), settings)
        try:
            shared.values.update(phase='抓取', time=0, qpos='shadow', epos='shadow')
            assert policy.policy(chunk, images(), runtime, state={'qpos': [1]}) is chunk
            assert shared.reads == 1
            runtime.consumed_actions = 5
            policy.policy(chunk, {}, runtime, state={})
            assert shared.reads == 1

            shared.values.update(phase='苹果{$time}{index}', time=20, info={'色': ['红', False]},
                                 bad=object(), nan=float('nan'), none=None, flag=False)
            runtime.consumed_actions = 60
            state = {'qpos': [2], 'eef': [3], 'qvel': [4], 'effort': [5], 'timestamp': 6}
            policy.policy(chunk, images(), runtime, state=state)
            assert shared.reads == 2
            state['qpos'][0] = 999
            shared.values['phase'] = 'changed after submit'
            done(policy)
            body = server.requests[0]['body']
            if kind == 'deepseek':
                instructions, content = (message['content'] for message in body['messages'])
            else:
                question, = body['questions']
                instructions, content = question['instructions'], body['input'][0]['content']
                if kind.endswith('choice'):
                    assert question['choices'][0]['description'] == '选项 苹果{$time}{index}'
                if kind.endswith('score'):
                    assert question['levels'][0]['description'] == '等级 苹果{$time}{index}'
            assert instructions == ('v={"色":["红",false]};time=20;missing=;bad=;nan=;none=null;'
                                    'flag=false;pos=[3]/[3];vel=[4];eff=[5];stamp=6;JSON={"a":1}\n\n'
                                    '本轮=苹果{$time}{index}')
            assert [p['text'] for p in content if 'text' in p] == [
                '取苹果{$time}{index} / 苹果{$time}{index}', 'H1:0:[1]:抓取', 'head=0', 'left_wrist=0', 'right_wrist=0',
                'C2:60:[2]:苹果{$time}{index}', 'head=20', 'left_wrist=20', 'right_wrist=20']

            shared.fail = True
            runtime.consumed_actions = 120
            policy.policy(chunk, images(), runtime, state={'qpos': [7]})
            done(policy)
            body = server.requests[1]['body']
            content = body['messages'][1]['content'] if kind == 'deepseek' else body['input'][0]['content']
            text = [part['text'] for part in content if 'text' in part]
            assert text[0:3] == ['取 / ', 'H1:60:[2]:苹果{$time}{index}', 'head=20']
            assert text[5:7] == ['C2:120:[7]:', 'head=']
            assert shared.reads == 3 and runtime.finishes == []
        finally:
            policy.close()


def test_reserved_variables_and_escaped_input_fields_do_not_read_the_store():
    settings = provider_settings(task='姿态={$epos}/{$eef}', prompt='位={$qpos};速={$qvel}')
    settings['input_templates'].update(task='{task};时间={$timestamp}',
        history_snapshot='{{index}} {$qpos} {{$ignored}} {index}-{index}/{step}')
    runtime = Runtime()
    runtime.globals = shared = Shared()
    shared.values.update(qpos='from globals', epos='from globals', timestamp='from globals')
    provider = Provider()
    policy = FailurePolicy(manifest(), settings, provider=provider)
    try:
        chunk = [[0.] * 14]
        policy.policy(chunk, images(), runtime, state={'qpos': '{index}'})
        runtime.consumed_actions = 60
        policy.policy(chunk, images(), runtime, state={'eef': [3, 4]})
        done(policy)
        batch, = provider.calls
        assert batch.text.task == '姿态=[3,4]/[3,4];时间='
        assert batch.text.instructions.startswith('位=;速=\n\n')
        assert '1'.join(batch[0].captions.caption) == '{index} {index} {$ignored} 1-1/0'
        assert shared.reads == 0
    finally:
        policy.close()


@pytest.mark.parametrize('field', ['camera', 'history_snapshot', 'prompt', 'description'])
def test_repeated_variable_is_bounded_before_a_frame_or_request_is_queued(monkeypatch, field):
    from rc_llm_failure import prompt_variables
    from rc_llm_failure.errors import LocalPolicyError

    monkeypatch.setattr(prompt_variables, 'MAX_REQUEST_BYTES', 512, raising=False)
    settings = provider_settings(profile='providers/gpt-decision-choice.yaml.example',
                                 prompt='检查', response_instructions='')
    large = '{$note}' * (2 if field == 'camera' else 7)
    if field in ('camera', 'history_snapshot'):
        settings['input_templates'][field] = large
    elif field == 'description':
        settings['choices'][0]['description'] = large
    else:
        settings['prompt'] = large
    runtime = Runtime()
    runtime.globals = Shared()
    runtime.globals.values['note'] = '苹' * 16  # 96 bytes in an ASCII-escaped JSON string
    provider = Provider()
    policy = FailurePolicy(manifest(), settings, provider=provider)
    chunk = [[0.] * 14]
    try:
        if field in ('prompt', 'description'):
            policy.policy(chunk, images(), runtime)
            runtime.consumed_actions = 60
        before = tuple(policy._history)
        with pytest.raises(LocalPolicyError, match='32MiB'):
            policy.policy(chunk, images(), runtime)
        assert policy.pending is None and not provider.calls
        assert tuple(policy._history) == before
    finally:
        policy.close()


def test_reset_drops_old_text_from_history_but_inflight_frames_survive_until_completion():
    pytest.importorskip('openai')
    gate = threading.Event()
    runtime = Runtime()
    runtime.globals = shared = Shared()
    chunk = [[0.] * 14]
    with model_server() as server:
        settings = provider_settings(base_url=server.url, prompt='阶段={$phase}', timeout_s=10.)
        settings['input_templates']['camera'] = '{camera}/{$phase}'
        server.responses.extend([{'wait': gate, 'body': response_for('deepseek', {'failed': True})},
                                 {'body': response_for('deepseek', {'failed': False})}])
        policy = FailurePolicy(manifest(history_size=2), settings)
        reset = None
        try:
            shared.values['phase'] = '旧任务'
            policy.policy(chunk, images(), runtime)
            frame = weakref.ref(policy._history[0])
            runtime.consumed_actions = 60
            policy.policy(chunk, images(), runtime)
            assert server.received.wait(3)
            shared.values['phase'] = '新任务'
            runtime.consumed_actions = 120
            policy.policy(chunk, {}, runtime)
            assert shared.reads == 2

            reset = threading.Thread(target=policy.reset_context)
            reset.start()
            wait(lambda: not policy._history)
            assert frame() is not None and reset.is_alive()
            gate.set()
            reset.join(3)
            assert not reset.is_alive()
            wait(lambda: frame() is None)

            policy.policy(chunk, images(), runtime)
            runtime.consumed_actions = 180
            policy.policy(chunk, images(), runtime)
            done(policy)
            policy.policy(chunk, {}, runtime)
            body = server.requests[1]['body']
            captions = [p['text'] for p in body['messages'][1]['content'] if 'text' in p]
            assert captions.count('head/新任务') == 2 and 'head/旧任务' not in captions
            assert body['messages'][0]['content'].startswith('阶段=新任务\n\n')
            assert runtime.finishes == [] and shared.reads == 4
        finally:
            gate.set()
            if reset:
                reset.join(3)
            policy.close()


def test_only_the_request_owns_its_current_caption():
    settings = provider_settings()
    settings['input_templates']['current_snapshot'] = '当前={$volatile}'
    runtime = Runtime()
    runtime.globals = Shared()
    runtime.globals.values['volatile'] = 'only-needed-while-current'
    provider = Provider()
    policy = FailurePolicy(manifest(), settings, provider=provider)
    try:
        chunk = [[0.] * 14]
        policy.policy(chunk, images(), runtime)
        assert runtime.globals.reads == 0
        runtime.consumed_actions = 60
        policy.policy(chunk, images(), runtime)
        done(policy)
        current = provider.calls[0][-1]
        assert runtime.globals.reads == 1
        assert '2'.join(current.captions.caption) == '当前=only-needed-while-current'
        assert all('only-needed-while-current' not in '1'.join(frame.captions.caption) for frame in policy._history)
        assert policy._history[-1].images is current.images
    finally:
        policy.close()
