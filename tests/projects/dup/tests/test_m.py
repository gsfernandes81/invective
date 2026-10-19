"""The tests of `m`: one overload and the setter."""

from dup import m


def test_int():
    """An int dispatches to the `int` overload."""
    assert m.show(3) == "int"


def test_setter():
    """Setting `c` stores the value."""
    t = m.Temp()
    t.c = 5
    assert t._c == 5
