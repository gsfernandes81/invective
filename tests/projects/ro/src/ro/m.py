"""A size every test reads and none checks closely."""


def size() -> int:
    """`0` and `1` survive: every test asserts only `size() >= 0`. `-1` is killed."""
    return 2
