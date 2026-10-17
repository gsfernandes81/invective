"""Three tests of `triple`, each failing under every mutant."""

from early import triple


def test_one():
    """One tripled is three."""
    assert triple.triple(1) == 3


def test_two():
    """Two tripled is six."""
    assert triple.triple(2) == 6


def test_three():
    """Three tripled is nine."""
    assert triple.triple(3) == 9
