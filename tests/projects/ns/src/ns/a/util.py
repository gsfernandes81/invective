"""One of two `util` modules in the namespace package `ns`; the test imports this one."""


def helper() -> int:
    """Killed by every value: the test checks `helper()` is 5."""
    return 5
