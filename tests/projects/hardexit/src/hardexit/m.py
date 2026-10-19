"""Targets whose every mutant fails a test, after which the conftest exits with 3.

The engine refuses the run before any mutant. Its canary child fails a test too,
and the conftest exits before the child writes its verdict line, so the engine
reads the canary as survived. A mutant's child would end the same way, exiting 3
without a verdict, which the engine reports as unmeasured.
"""


def ready() -> bool:
    """Killed in plain pytest: `test_ready` checks that it is True."""
    return True


def code() -> int:
    """Killed by every value in plain pytest: `test_code` checks that it is 3."""
    return 3
