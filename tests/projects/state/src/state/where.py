"""Functions tests call after changing the process's working directory or environment.

The tests leave the change in place, so every test after them in the same process
runs with it too.
"""

from __future__ import annotations


def label(name: str) -> str:
    """Killed by both values: a test calls it after moving to another directory."""
    return f"<{name}>"


def size(items: list[int]) -> int:
    """Killed by every value: a test calls it after emptying `os.environ`."""
    return len(items)
