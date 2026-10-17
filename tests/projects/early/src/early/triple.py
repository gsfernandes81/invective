def triple(n: int) -> int:
    """Every value is killed by each of the three tests that call it, so
    a child that stops at the first failure runs one of them only."""
    return 3 * n
