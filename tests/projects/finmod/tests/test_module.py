"""A module fixture that checks `closed()` after its yield, and a slow and a fast test."""

import time

import pytest
from finmod import m


@pytest.fixture(scope="module")
def door():
    """Yield nothing, then assert the door closed as 4."""
    yield
    assert m.closed() == 4


@pytest.mark.usefixtures("door")
def test_slow():
    """Costs more than `test_fast` in the baseline."""
    time.sleep(0.2)


@pytest.mark.usefixtures("door")
def test_fast():
    """Costs little."""
