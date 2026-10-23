"""Composition must preserve the policy contract and own every child lifecycle."""

from types import SimpleNamespace

import numpy as np
import pytest

import rc_policy_sdk
from rc_policy_sdk.configuration import apply_patch


def chain(stages):
    # Keep the missing public API a test failure instead of a collection error.
    assert hasattr(rc_policy_sdk, "PolicyChain"), "SDK must expose PolicyChain"
    return rc_policy_sdk.PolicyChain(stages)


def stage(**callbacks):
    return SimpleNamespace(
        **{
            "initialize": lambda config: {"config": config, "calls": 0},
            "policy": lambda context, state, action_chunk, images, config: action_chunk,
            "reset_context": lambda context: context.update(calls=0),
            "release": lambda context: None,
            **callbacks,
        }
    )


@pytest.mark.parametrize("mode", ["qpos", "epos", "mixed"])
def test_stages_receive_previous_output_and_unchanged_other_inputs(mode):
    config = {"params": {}, "execution": {"control_mode": mode}}
    state = {"qpos": [0.0] * 14, "eef": [1.0] * 14}
    images = {"head": np.zeros((2, 3, 3), np.uint8)}
    images["head"].flags.writeable = False
    incoming = {"actions": [], "modes": []} if mode == "mixed" else []
    first_output = np.full((2, 14), 2.0, dtype=np.float64)
    final_output = np.full((1, 14), 3.0, dtype=np.float64)
    if mode == "mixed":
        first_output = {"actions": first_output, "modes": ["qpos", "epos"]}
        final_output = {"actions": final_output, "modes": ["epos"]}
    contexts, seen = [], []

    def initialize(received_config):
        assert received_config is config
        context = {"index": len(contexts)}
        contexts.append(context)
        return context

    def step(context, received_state, action_chunk, received_images, received_config):
        assert received_state is state and received_images is images
        assert received_config is config
        index = context["index"]
        assert context is contexts[index]
        assert action_chunk is (incoming if index == 0 else first_output)
        seen.append(index)
        return first_output if index == 0 else final_output

    pipeline = chain([stage(initialize=initialize, policy=step), stage(initialize=initialize, policy=step)])
    context = pipeline.initialize(config)
    try:
        assert pipeline.policy(context, state, incoming, images, config) is final_output
        assert seen == [0, 1]
        assert contexts[0] is not contexts[1]
    finally:
        pipeline.release(context)


def test_runtime_and_hot_config_reference_reach_children():
    runtime = object()
    config = {"params": {"value": 2.0}, "hot_reloadable": ["params.value"]}
    seen = []

    def initialize(settings, *, runtime):
        seen.append(runtime)
        return settings

    def policy(context, state, action_chunk, images, settings, *, runtime):
        assert settings is context is config
        seen.append(runtime)
        return np.full((1, 14), context["params"]["value"])

    pipeline = chain([stage(initialize=initialize, policy=policy), stage()])
    context = pipeline.initialize(config, runtime=runtime)
    try:
        apply_patch(config, {"params.value": 7.0})
        result = pipeline.policy(context, {}, [], {}, config, runtime=runtime)
        np.testing.assert_array_equal(result, [[7.0] * 14])
        assert seen == [runtime, runtime]
    finally:
        pipeline.release(context)


def test_single_stage_and_nested_chains_keep_independent_contexts():
    contexts = []

    def initialize(config):
        context = {"calls": 0}
        contexts.append(context)
        return context

    def policy(context, state, action_chunk, images, config):
        context["calls"] += 1
        return np.asarray(action_chunk) + context["calls"]

    inner = chain([stage(initialize=initialize, policy=policy)])
    outer = chain([inner, inner])
    context = outer.initialize({})
    try:
        np.testing.assert_array_equal(outer.policy(context, {}, [[0] * 14], {}, {}), [[2] * 14])
        np.testing.assert_array_equal(outer.policy(context, {}, [[0] * 14], {}, {}), [[4] * 14])
        assert len(contexts) == 2 and contexts[0] is not contexts[1]
        outer.reset_context(context)
        np.testing.assert_array_equal(outer.policy(context, {}, [[0] * 14], {}, {}), [[2] * 14])
    finally:
        outer.release(context)


@pytest.mark.parametrize("bad_output", [[], [[0] * 13], [[float("nan")] * 14], [[float("inf")] * 14]])
def test_invalid_intermediate_actions_stop_before_next_stage(bad_output):
    called = []
    pipeline = chain([
        stage(policy=lambda *args: bad_output),
        stage(policy=lambda *args: called.append(True)),
    ])
    context = pipeline.initialize({})
    try:
        with pytest.raises(RuntimeError, match="stage 1.*policy"):
            pipeline.policy(context, {}, [], {}, {})
        assert called == []
    finally:
        pipeline.release(context)


def test_stage_error_preserves_cause_and_stops_chain():
    failure = ValueError("model failed")
    called = []

    def fail(*args):
        raise failure

    pipeline = chain([stage(policy=fail), stage(policy=lambda *args: called.append(True))])
    context = pipeline.initialize({})
    try:
        with pytest.raises(RuntimeError, match="stage 1.*model failed") as caught:
            pipeline.policy(context, {}, [], {}, {})
        assert caught.value.__cause__ is failure
        assert called == []
    finally:
        pipeline.release(context)


@pytest.mark.parametrize("interrupt", [False, True])
def test_failed_initialization_releases_failed_and_started_children_in_reverse(interrupt):
    events = []
    failure = KeyboardInterrupt() if interrupt else ValueError("load failed")

    def fail(config):
        events.append("init second")
        raise failure

    pipeline = chain([
        stage(initialize=lambda config: events.append("init first") or "first",
              release=lambda context: events.append(("release first", context))),
        stage(initialize=fail, release=lambda context: events.append(("release second", context))),
        stage(initialize=lambda config: events.append("init third")),
    ])
    with pytest.raises(KeyboardInterrupt if interrupt else RuntimeError):
        pipeline.initialize({})
    pipeline.release(None)
    assert events == ["init first", "init second", ("release second", None), ("release first", "first")]


def test_initialization_error_remains_visible_when_rollback_also_fails():
    released = []

    def fail_init(config):
        raise ValueError("load failed")

    def fail_release(context):
        released.append("second")
        raise ValueError("cleanup failed")

    pipeline = chain([
        stage(release=lambda context: released.append("first")),
        stage(initialize=fail_init, release=fail_release),
    ])
    with pytest.raises(RuntimeError) as caught:
        pipeline.initialize({})
    assert "load failed" in str(caught.value) and "cleanup failed" in str(caught.value)
    assert released == ["second", "first"]


def test_reset_attempts_every_stage_and_release_is_reverse_order_and_idempotent():
    events = []

    def fail_reset(context):
        events.append("reset first")
        raise ValueError("reset failed")

    def fail_release(context):
        events.append("release second")
        raise ValueError("cleanup failed")

    pipeline = chain([
        stage(reset_context=fail_reset, release=lambda context: events.append("release first")),
        stage(reset_context=lambda context: events.append("reset second"), release=fail_release),
    ])
    context = pipeline.initialize({})
    with pytest.raises(RuntimeError, match="reset failed"):
        pipeline.reset_context(context)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        pipeline.release(context)
    pipeline.release(context)
    assert events == ["reset first", "reset second", "release second", "release first"]
    with pytest.raises(RuntimeError, match="released"):
        pipeline.policy(context, {}, [[0] * 14], {}, {})
    with pytest.raises(RuntimeError, match="released"):
        pipeline.reset_context(context)


def test_optional_observation_callbacks_respect_child_opt_out_and_runtime():
    calls = []
    config, state, images, runtime = {}, {}, {}, object()

    def observe(context, received_state, received_images, received_config, *, runtime):
        assert received_state is state and received_images is images and received_config is config
        calls.append(runtime)

    pipeline = chain([
        stage(),
        stage(observe=observe),
        stage(observe=lambda *args: calls.append("disabled"), wants_observations=lambda context: False),
    ])
    context = pipeline.initialize(config)
    try:
        assert pipeline.wants_observations(context)
        pipeline.observe(context, state, images, config, runtime=runtime)
        assert calls == [runtime]
    finally:
        pipeline.release(context)
    plain = chain([stage()])
    plain_context = plain.initialize({})
    try:
        assert not plain.wants_observations(plain_context)
    finally:
        plain.release(plain_context)


def test_all_config_validators_run_before_any_initialization():
    events = []

    def validate(config):
        events.append("validate")
        if config["params"]["size"] <= 0:
            raise ValueError("size must be positive")

    pipeline = chain([
        stage(initialize=lambda config: events.append("init")),
        stage(validate_config=validate),
    ])
    with pytest.raises(RuntimeError, match="stage 2.*validate_config.*positive"):
        pipeline.initialize({"params": {"size": 0}})
    assert events == ["validate"]
    pipeline.validate_config({"params": {"size": 1}})
    assert events == ["validate", "validate"]


def test_empty_chain_and_missing_or_async_callbacks_are_rejected_before_initializing():
    with pytest.raises(ValueError, match="at least one"):
        chain([])
    with pytest.raises(ValueError, match="stage 2.*initialize"):
        chain([stage(), SimpleNamespace()])

    async def async_policy(*args):
        return [[0] * 14]

    with pytest.raises(ValueError, match="stage 1.*synchronous"):
        chain([stage(policy=async_policy)])


def test_context_cannot_be_used_with_another_chain():
    first, second = chain([stage()]), chain([stage()])
    context = first.initialize({})
    try:
        with pytest.raises(ValueError, match="context"):
            second.policy(context, {}, [[0] * 14], {}, {})
        with pytest.raises(ValueError, match="context"):
            second.release(context)
    finally:
        first.release(context)
