"""A module first imported by a test, after the test moved to another directory."""

from __future__ import annotations


def mark() -> str:
    """Killed by both values: the test that imports the module reads it."""
    return "late"
