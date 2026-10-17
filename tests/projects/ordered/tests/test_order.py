"""`test_read` depends on `test_fill` having run first.

With `-k "fill or read"`, `test_read` is the only test that runs the target, so the
canary child runs it alone, and it fails on the missing key before it calls the
target.
"""

from ordered import thing

FILLED = {}


def test_fill():
    """Put the key `test_read` reads."""
    FILLED["n"] = 1


def test_read():
    """Read the key `test_fill` put, and pass it to the target."""
    n = FILLED["n"]
    assert thing.value(n) == 2


def test_direct():
    """Call the target with no dependency on another test."""
    assert thing.value(1) == 2
