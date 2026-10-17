import pytest

from fixtures import calc


@pytest.fixture(scope="session")
def db():
    return calc.base()


@pytest.fixture
def dyn():
    return calc.dynamic()
