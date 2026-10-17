"""Functions only fixtures call.

Each is called through a pattern the map follows from the fixture to the test that
uses it.
"""

from __future__ import annotations


def base() -> int:
    """Killed by every value, by the test that uses the override of `db`.

    The conftest's `db` calls it, and a module's `db(db)` overrides that fixture.
    """
    return 5


def dynamic() -> int:
    """Killed by every value, by the test that asks for `dyn` in its body.

    `dyn` calls it, and the test asks for `dyn` with `getfixturevalue`.
    """
    return 8
