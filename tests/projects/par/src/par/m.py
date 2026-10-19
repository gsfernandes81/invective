"""Four functions one test calls without checking an answer, so every mutant survives.

Plain pytest, and the engine with one worker, report every mutant survived. With
four workers the engine runs the test in four children at once, one per mutant,
each of which sees the others and fails, so it reports kills that no test gives the
mutant. With one mutant per worker the four children start together, so each
overlaps every other.
"""


def f1() -> bool:
    """Survives: `test_alone` calls it and ignores the answer; killed by four workers."""
    return True


def f2() -> bool:
    """Survives: `test_alone` calls it and ignores the answer; killed by four workers."""
    return True


def f3() -> bool:
    """Survives: `test_alone` calls it and ignores the answer; killed by four workers."""
    return True


def f4() -> bool:
    """Survives: `test_alone` calls it and ignores the answer; killed by four workers."""
    return True
