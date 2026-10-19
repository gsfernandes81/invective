"""A test whose cost falls between `test_a`'s two."""

import time

from ro import m


def test_b_mid():
    """The size is not negative, after a shorter delay."""
    time.sleep(0.1)
    assert m.size() >= 0
