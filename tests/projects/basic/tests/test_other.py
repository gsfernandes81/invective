"""The tests of `other`, one of which also reaches `gate`."""

from basic import gate, other


def test_twice():
    """Two doubled is four."""
    assert other.twice(2) == 4


def test_sign_and_a_count():
    """One is positive, and two items do not count as one."""
    assert other.sign(1) is True
    assert gate.count(["a", "b"]) != 1
