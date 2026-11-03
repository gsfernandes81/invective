"""One test that uses the session fixture, and a later one that does not."""

import pytest
from fin2 import shop


@pytest.mark.usefixtures("shop_open")
def test_uses_shop():
    """`other()` is 1."""
    assert shop.other() == 1


def test_z_last_without_shop():
    """Runs last, so the session fixture is torn down after it."""
