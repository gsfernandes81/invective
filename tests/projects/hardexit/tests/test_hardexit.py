"""The tests of `hardexit.m`."""

from hardexit import m


def test_ready():
    """The target is ready."""
    assert m.ready() is True


def test_code():
    """The code is 3."""
    assert m.code() == 3
