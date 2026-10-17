"""Imported only inside `test_late`, so it is first imported in a child,
where the mutant is already live: `compute`, called at the module's
import, is measured, and every value is killed."""


def compute() -> int:
    return 7


VALUE = compute()
