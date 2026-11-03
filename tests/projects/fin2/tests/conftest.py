"""The fin2 project's session fixture, which calls `shop.closing` at teardown."""

import pytest
from fin2 import shop


@pytest.fixture(scope="session")
def shop_open():
    """Yield nothing, then fail the session unless the shop closes "ok"."""
    yield
    if shop.closing() != "ok":
        pytest.fail("the shop did not close")
