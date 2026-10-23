"""Compose ordinary policy modules inside a single worker."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import inspect

from .actions import normalize_actions
from .runtime import invoke


@dataclass
class _ChainContext:
    owner: PolicyChain
    children: list = field(default_factory=list)
    released: bool = False


@contextmanager
def _stage_call(index, operation):
    try:
        yield
    except Exception as exc:
        raise RuntimeError(f"policy chain stage {index + 1} {operation}: {exc}") from exc


def _raise_errors(errors):
    if errors:
        # Finish the lifecycle for every child, but preserve shutdown signals.
        for error in errors:
            if not isinstance(error, Exception):
                raise error
        raise RuntimeError("; ".join(str(error) for error in errors)) from errors[0]


class PolicyChain:
    """A policy with the same callbacks as each of its ordered child policies.

    Each child is a module or object implementing initialize, policy,
    reset_context and release. All children receive the same config, state,
    images and optional runtime. Only action_chunk changes: a child's exact
    return value becomes the next child's input. Every output must satisfy
    the configured qpos/epos/mixed action contract.

    initialize returns an opaque context owned by this chain. Callbacks follow
    the worker's existing concurrency rules; children must synchronize their
    own shared state when using observation callbacks or local commands.
    """

    def __init__(self, stages):
        self.stages = tuple(stages)
        if not self.stages:
            raise ValueError("PolicyChain requires at least one stage")
        required = ("initialize", "policy", "reset_context", "release")
        optional = ("observe", "wants_observations", "validate_config")
        for index, stage in enumerate(self.stages):
            for name in required + optional:
                callback = getattr(stage, name, None)
                if callback is None and name in optional:
                    continue
                if (not callable(callback) or inspect.iscoroutinefunction(callback)
                        or inspect.isgeneratorfunction(callback) or inspect.isasyncgenfunction(callback)):
                    raise ValueError(f"policy chain stage {index + 1} {name} must be a synchronous function")

    def _check_context(self, context, *, allow_released=False):
        if not isinstance(context, _ChainContext) or context.owner is not self:
            raise ValueError("context belongs to a different PolicyChain")
        if context.released and not allow_released:
            raise RuntimeError("policy chain context has been released")

    def initialize(self, config, *, runtime=None):
        self.validate_config(config)
        context = _ChainContext(self)
        try:
            for index, stage in enumerate(self.stages):
                # Match the worker contract: failed initialize gets release(None).
                context.children.append(None)
                with _stage_call(index, "initialize"):
                    context.children[index] = invoke(stage.initialize, (config,), runtime)
        except BaseException as failure:
            try:
                self.release(context)
            except Exception as cleanup_error:
                if isinstance(failure, Exception):
                    raise RuntimeError(f"{failure}; rollback failed: {cleanup_error}") from failure
            raise
        return context

    def policy(self, context, state, action_chunk, images, config, *, runtime=None):
        self._check_context(context)
        mode = config.get("execution", {}).get("control_mode", "qpos")
        for index, stage in enumerate(self.stages):
            with _stage_call(index, "policy"):
                action_chunk = invoke(
                    stage.policy, (context.children[index], state, action_chunk, images, config), runtime,
                )
                # Validate without replacing the object passed to the next stage.
                normalize_actions(action_chunk, mode)
        return action_chunk

    def validate_config(self, config):
        """Validate every child before initialization or a worker hot update."""
        for index, stage in enumerate(self.stages):
            validator = getattr(stage, "validate_config", None)
            if validator is not None:
                with _stage_call(index, "validate_config"):
                    validator(config)

    def _observes(self, index, context):
        stage = self.stages[index]
        if getattr(stage, "observe", None) is None:
            return False
        predicate = getattr(stage, "wants_observations", None)
        with _stage_call(index, "wants_observations"):
            return predicate(context) if predicate is not None else True

    def wants_observations(self, context):
        self._check_context(context)
        return any(self._observes(index, child) for index, child in enumerate(context.children))

    def observe(self, context, state, images, config, *, runtime=None):
        """Broadcast an observation to children that request it."""
        self._check_context(context)
        for index, child in enumerate(context.children):
            if self._observes(index, child):
                with _stage_call(index, "observe"):
                    invoke(self.stages[index].observe, (child, state, images, config), runtime)

    def reset_context(self, context):
        self._check_context(context)
        errors = []
        for index, child in enumerate(context.children):
            try:
                with _stage_call(index, "reset_context"):
                    self.stages[index].reset_context(child)
            except BaseException as exc:
                errors.append(exc)
        _raise_errors(errors)

    def release(self, context):
        """Release started children in reverse order, attempting each once."""
        if context is None:
            return
        self._check_context(context, allow_released=True)
        if context.released:
            return
        context.released = True
        children = list(enumerate(context.children))
        context.children.clear()
        errors = []
        for index, child in reversed(children):
            try:
                with _stage_call(index, "release"):
                    self.stages[index].release(child)
            except BaseException as exc:
                errors.append(exc)
        _raise_errors(errors)
