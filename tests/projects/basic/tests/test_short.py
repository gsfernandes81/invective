"""The test of `short`'s one-line functions."""

from basic import short


def test_one_line_bodies():
    """Each one-line function returns its value."""
    assert short.one() == 3
    assert short.Box().get() == 4
