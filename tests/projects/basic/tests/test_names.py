"""The tests of `names`: one through a second module name, one after a reload."""

import importlib
import os

from basic import names


def test_the_second_name(monkeypatch):
    """The file imported as the top-level `names` is a module of its own, and greets."""
    monkeypatch.syspath_prepend(os.path.dirname(names.__file__))
    again = importlib.import_module("names")
    assert again is not names
    assert again.greet("ann") == "hello ann"


def test_after_a_reload():
    """`basic.names` reloaded is the same module object, run again, and parts."""
    reloaded = importlib.reload(names)
    assert reloaded is names
    assert reloaded.part("bo") == "goodbye bo"
