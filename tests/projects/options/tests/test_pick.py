"""The tests of `pick`; the project's `addopts` leave out the last two."""

from options import pick


def test_first():
    """The first of 7 and 8 is 7."""
    assert pick.first([7, 8]) == 7


def test_first_again():
    """The first of 9 is 9."""
    assert pick.first([9]) == 9


def test_unselected():
    """Left out by `-k`."""
    assert pick.unselected()


def test_deselected():
    """Left out by `--deselect`."""
    assert pick.deselected()
