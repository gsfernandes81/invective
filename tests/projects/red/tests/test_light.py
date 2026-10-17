"""A passing test and a failing one, so the baseline is red."""

from red import light


def test_stop():
    """`stop` is true."""
    assert light.stop()


def test_wrong():
    """Fails: it prints a line for the refusal to show, then asserts `stop` is false."""
    print("the light is red")
    assert light.stop() is False
