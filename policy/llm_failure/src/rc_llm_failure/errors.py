"""Recoverable supplier request failures, distinct from local faults and verdicts."""


class ProviderError(RuntimeError):
    pass


class LocalPolicyError(RuntimeError):
    """An actionable local fault which must never become supplier recovery."""
