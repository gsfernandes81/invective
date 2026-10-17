"""`test_read` depends on `test_fill` having run first. With `-k "fill or
read"`, `test_read` is the only test that runs the target, so the canary
child runs it alone, and it fails on the missing key before it calls the
target."""

from ordered import thing

FILLED = {}


def test_fill():
    FILLED["n"] = 1


def test_read():
    n = FILLED["n"]
    assert thing.value(n) == 2


def test_direct():
    assert thing.value(1) == 2
