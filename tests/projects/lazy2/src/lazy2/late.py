"""A module first imported inside `test_a_imports_first`, which calls `compute` at import.

The value `compute` returns is checked only by a later test, which finds the module
already imported.
"""


def compute() -> int:
    """Killed by every value: `test_b_reads_value` checks `VALUE`, which it made, is 7."""
    return 7


VALUE = compute()


def other() -> int:
    """`0` and `-1` are killed by the first test; `1` is its own body, so no mutant."""
    return 1
