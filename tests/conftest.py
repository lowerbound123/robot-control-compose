import pytest


@pytest.fixture(autouse=True)
def close_test_hosts():
    yield
    from host_helpers import cleanup_hosts
    cleanup_hosts()
