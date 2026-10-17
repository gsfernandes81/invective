import pytest

from finalizer import shop


@pytest.fixture(scope="session")
def shop_open():
    yield
    if shop.closing() != "ok":
        pytest.fail("the shop did not close")
