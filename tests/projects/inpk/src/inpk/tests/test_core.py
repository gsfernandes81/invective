"""The tests of the package's `__init__` and of `core`."""

from inpk import base, core


def test_base():
    """`base()` is 10."""
    assert base() == 10


def test_name():
    """`core.name()` is "core"."""
    assert core.name() == "core"
