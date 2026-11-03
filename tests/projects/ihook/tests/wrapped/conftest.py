"""A `pytest_runtest_protocol` wrapper that names the running test in the environment.

Plain pytest calls the hook through the session's hook relay, which holds every
conftest's implementation, so the wrapper runs for the tests of sibling directories
too.
"""

import os
from collections.abc import Generator

import pytest


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    """Set `IHOOK_NODE` to the item's node id, then run the item."""
    os.environ["IHOOK_NODE"] = item.nodeid
    return (yield)
