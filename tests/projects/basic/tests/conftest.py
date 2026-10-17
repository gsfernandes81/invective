"""The basic project's fixtures: a session fixture that runs a target, and pid records."""

import os

import pytest
from basic import gate


@pytest.fixture(scope="session")
def doubled() -> int:
    """Return `gate.through_fixture(21)`, the only call of that function."""
    return gate.through_fixture(21)


@pytest.fixture(autouse=True)
def record_pid() -> None:
    """Append the pid each test runs in to the file `BASIC_PIDS` names, when set.

    The suite reads the file to show that no test runs in the parent.
    """
    where = os.environ.get("BASIC_PIDS")
    if where:
        with open(where, "a") as fh:
            fh.write(f"{os.getpid()}\n")
