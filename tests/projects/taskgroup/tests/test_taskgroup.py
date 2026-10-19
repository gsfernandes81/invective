"""The test of `taskgroup.m`, which gathers its coroutines in a task group."""

import asyncio

from taskgroup import m


async def _total() -> int:
    """Return the sum of `fetch` of 1, 2 and 3, each run as a task of one group."""
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(m.fetch(n)) for n in (1, 2, 3)]
    return sum(task.result() for task in tasks)


def test_sum():
    """The fetched values of 1, 2 and 3 sum to 6."""
    assert asyncio.run(_total()) == 6
