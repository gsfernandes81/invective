import pytest


@pytest.fixture(scope="module")
def db(db):
    return db + 1


def test_the_override_sees_the_base(db):
    assert db == 6
