class ProviderError(RuntimeError):
    """A sanitized error suitable for display; never contains an API response body."""
