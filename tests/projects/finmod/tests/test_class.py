"""A class-scoped fixture that checks `cleared()` after its yield."""

import pytest
from finmod import m


@pytest.fixture(scope="class")
def table():
    """Yield nothing, then assert the table cleared as 2."""
    yield
    assert m.cleared() == 2


@pytest.mark.usefixtures("table")
class TestTable:
    """Two tests that share one set-up of `table`."""

    def test_first(self):
        """Uses the table."""

    def test_second(self):
        """Uses it again."""
