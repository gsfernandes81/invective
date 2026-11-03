"""A coroutine function and an async generator, which the tests run with `asyncio`.

A mutant replaces the body inside the `async def`, so a mutated coroutine function
still returns a coroutine, and a mutated async generator still iterates. The plain
generator is `gate.evens`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


async def is_even(n: int) -> bool:
    """Killed by both values: the test awaits it for 2 and for 3."""
    await asyncio.sleep(0)
    return n % 2 == 0


async def alternating(count: int) -> AsyncIterator[bool]:
    """Killed by every value: the test collects `[True, False]` from it."""
    for n in range(count):
        await asyncio.sleep(0)
        yield n % 2 == 0
