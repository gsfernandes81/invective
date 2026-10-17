"""A target with a lambda, which runs a line of the module but is no function of it."""

VALUE = 3
double = lambda x: x * 2


def value() -> int:
    """Return `VALUE`; no test calls it."""
    return VALUE
