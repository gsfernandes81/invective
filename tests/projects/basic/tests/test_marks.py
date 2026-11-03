"""The tests of `marks`: a strict `xfail`, and a test behind a fixture that skips."""

import pytest
from basic import marks


@pytest.mark.xfail(strict=True, reason="Monday is not the weekend")
def test_monday_is_the_weekend():
    """Fails as written, which the strict `xfail` expects."""
    assert marks.is_weekend("mon")


@pytest.fixture
def open_shop() -> None:
    """Skip the test that asks for it when `marks.in_stock` says there is no stock."""
    if not marks.in_stock(3):
        pytest.skip("nothing in stock")


@pytest.mark.usefixtures("open_shop")
def test_sunday_is_the_weekend():
    """Sunday is the weekend."""
    assert marks.is_weekend("sun")
