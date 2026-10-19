"""The target file's path, and its restoration when the session ends.

A run in the checkout would otherwise leave the edit `test_save_mid_run` makes. Only
the process that imported this conftest restores the file: under invective that is
the parent, after every child has run, and a forked child that ends its session
leaves the edit in place.
"""

import os

import pytest

_TARGET = os.path.join(os.path.dirname(__file__), "..", "src", "edited", "m.py")
with open(_TARGET, encoding="utf-8") as _fh:
    _ORIGINAL = _fh.read()
_PID = os.getpid()


@pytest.fixture
def target() -> str:
    """Return the path of the target file, `src/edited/m.py`."""
    return _TARGET


def pytest_unconfigure() -> None:
    """Write the target back as collection read it, in the process that read it."""
    if os.getpid() == _PID:
        with open(_TARGET, "w", encoding="utf-8") as fh:
            fh.write(_ORIGINAL)
