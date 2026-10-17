"""Functions only fixtures call, each through a pattern the map must
follow from the fixture to the test that uses it."""

from __future__ import annotations


def base() -> int:
    """Called by the conftest's `db`, which a module's `db(db)` overrides:
    every value is killed by the test that uses the override."""
    return 5


def dynamic() -> int:
    """Called by `dyn`, which a test asks for with `getfixturevalue` in its
    body: every value is killed by that test."""
    return 8
