import os

import pytest

from basic import gate


@pytest.fixture(scope="session")
def doubled():
    return gate.through_fixture(21)


@pytest.fixture(autouse=True)
def record_pid(tmp_path_factory):
    """Every test writes the pid it ran in, so the suite can show the
    parent never ran one."""
    where = os.environ.get("BASIC_PIDS")
    if where:
        with open(where, "a") as fh:
            fh.write("%d\n" % os.getpid())
    yield
