"""The tests of `m`: the constant the getter made, and the setter."""

from propimp import m


def test_default():
    """The constant read at import is 3."""
    assert m.DEFAULT == 3


def test_setter():
    """Setting `v` stores the value."""
    c = m.C()
    c.v = 8
    assert c._v == 8
