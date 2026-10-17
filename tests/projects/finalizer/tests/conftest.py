"""A session fixture whose finalizer calls the target."""

from collections.abc import Generator

import pytest
from finalizer import shop


@pytest.fixture(scope="session")
def shop_open() -> Generator[None]:
    """Fail the session's last teardown if `shop.closing` does not answer "ok"."""
    yield
    if shop.closing() != "ok":
        pytest.fail("the shop did not close")
