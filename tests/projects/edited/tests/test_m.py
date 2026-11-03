"""Tests of `edited.m`, which import it inside their bodies."""


def test_total():
    """One and two total three."""
    from edited import m

    assert m.total([1, 2]) == 3


def test_greeting():
    """The greeting is "hi"."""
    from edited import m

    assert m.greeting() == "hi"
