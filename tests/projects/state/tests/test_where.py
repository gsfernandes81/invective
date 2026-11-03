"""The tests of `where` and `late`, which change the directory and the environment.

Neither change is undone, so each test after the one that makes it runs with it.
"""

import importlib
import os

from state import where


def test_after_a_chdir(tmp_path):
    """Moved to another directory, a module first imported now is served as usual."""
    os.chdir(tmp_path)
    assert where.label("a") == "<a>"
    assert importlib.import_module("state.late").mark() == "late"


def test_after_the_environment_is_cleared():
    """With `os.environ` emptied, two items are two."""
    os.environ.clear()
    assert where.size([1, 2]) == 2


def test_after_both():
    """The label of `b` is `<b>`, in whatever directory and environment are left."""
    assert where.label("b") == "<b>"
