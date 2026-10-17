"""A module fixture that overrides the conftest's `db` and asks for it by name."""

import pytest


@pytest.fixture(scope="module")
def db(db: int) -> int:
    """Return the conftest's `db` plus one."""
    return db + 1


def test_the_override_sees_the_base(db: int):
    """The override's value is the base's plus one."""
    assert db == 6
