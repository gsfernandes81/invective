"""Functions each test runs in another process: a pool worker, a fork, an interpreter.

Plain pytest fails a test for every mutant here. The engine traces only the process
a test runs in, so it sees no test reach the three that run elsewhere.
"""


def in_process() -> bool:
    """Killed: `test_in_process` calls it in the test's own process.

    Without one line of the targets run in the test's process, the engine refuses
    the run as one that runs no line of its targets.
    """
    return True


def in_pool(x: int) -> int:
    """Killed by every value: `test_pool` maps it over a fork-context pool.

    The engine reports every value unreached: a pool's workers are forked from the
    test's process, and the tracer does not follow them, so no test covers it.
    """
    return x * 2


def in_fork() -> int:
    """Killed by every value: `test_os_fork` forks a child that writes it to a pipe.

    The engine reports every value unreached, as for `in_pool`: the tracer does not
    follow the forked child.
    """
    return 5


def in_subprocess() -> int:
    """Killed by every value: `test_subprocess` prints it from a fresh interpreter.

    The engine reports every value unreached. A fresh interpreter has no finder, so
    it runs the code as written and no mutant is live in it: a documented limitation
    of 1.0, not a bug.
    """
    return 9
