"""The tests of `kinds`, which run its coroutines with `asyncio.run`."""

import asyncio

from basic import kinds


def test_awaited():
    """2 is even and 3 is not."""
    assert asyncio.run(kinds.is_even(2)) is True
    assert asyncio.run(kinds.is_even(3)) is False


def test_iterated():
    """Two values alternate from `True`."""

    async def collect() -> list[bool]:
        return [flag async for flag in kinds.alternating(2)]

    assert asyncio.run(collect()) == [True, False]
