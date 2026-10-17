"""The one test of the threads project."""

from threads import thing


def test_value():
    """`value` is 2."""
    assert thing.value() == 2
