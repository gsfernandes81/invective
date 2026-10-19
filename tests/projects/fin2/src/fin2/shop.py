"""A shop a session fixture opens and checks at its close."""


def closing() -> str:
    """Killed by both values: the session fixture's teardown fails unless it is "ok".

    The teardown runs after the last test, which does not use the fixture, and the
    error is reported against that test.
    """
    return "ok"


def other() -> int:
    """`0` and `-1` are killed by the test; `1` is its own body, so no mutant."""
    return 1
