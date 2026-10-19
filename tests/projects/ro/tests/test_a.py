"""Two tests of one module fixture, one fast and one slow."""

import time

import pytest
from ro import m


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    """Return a directory made once per module; a second set-up raises."""
    return tmp_path_factory.mktemp("data", numbered=False)


@pytest.mark.usefixtures("data")
def test_a_fast():
    """The size is not negative."""
    assert m.size() >= 0


@pytest.mark.usefixtures("data")
def test_a_slow():
    """The size is not negative, after a delay."""
    time.sleep(0.2)
    assert m.size() >= 0
