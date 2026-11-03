"""The test of `ns.a.util`, which leaves `ns.b.util` unimported."""

from ns.a import util


def test_helper():
    """`helper` returns 5."""
    assert util.helper() == 5
