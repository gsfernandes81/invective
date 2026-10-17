"""The fixtures that call the targets."""

import pytest
from fixtures import calc


@pytest.fixture(scope="session")
def db() -> int:
    """Return `calc.base()`."""
    return calc.base()


@pytest.fixture
def dyn() -> int:
    """Return `calc.dynamic()`."""
    return calc.dynamic()
