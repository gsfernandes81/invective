"""A module imported only inside `test_late`, so first imported in a child.

The mutant is live there before the import, so `compute`, called at the module's
import, is measured, and every value is killed.
"""


def compute() -> int:
    """Return 7; the module calls it at import."""
    return 7


VALUE = compute()
