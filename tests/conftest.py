import os
from pathlib import Path
import sys

import pytest


# Project-level acceptance may compare policies in one interpreter; each business
# namespace still resolves to its own policy directory, never a shared package.
ROOT = Path(__file__).resolve().parents[1]
POLICY_SOURCES = [str(path) for path in sorted((ROOT/'policy').glob('*/src'))]
sys.path[:0] = POLICY_SOURCES
os.environ['PYTHONPATH'] = os.pathsep.join([*POLICY_SOURCES, os.environ.get('PYTHONPATH','')])


@pytest.fixture(autouse=True)
def close_test_hosts():
    yield
    from host_helpers import cleanup_hosts
    cleanup_hosts()
