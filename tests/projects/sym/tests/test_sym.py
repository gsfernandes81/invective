"""The tests of the linked module and of the plain one."""

from sym import alias, plain


def test_double():
    """Two doubled is four."""
    assert alias.double(2) == 4


def test_triple():
    """Two tripled is six."""
    assert plain.triple(2) == 6
