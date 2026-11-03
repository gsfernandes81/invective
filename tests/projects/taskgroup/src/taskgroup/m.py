"""A coroutine function the test runs as tasks of an `asyncio.TaskGroup`."""

import asyncio


async def fetch(n: int) -> int:
    """Killed by every value: `test_sum` checks that 1, 2 and 3 sum to 6.

    The engine refuses the run before any mutant: the canary raised in a task
    reaches the test wrapped in the task group's `ExceptionGroup`, so the canary
    child's first failure is not the canary's.
    """
    await asyncio.sleep(0)
    return n
