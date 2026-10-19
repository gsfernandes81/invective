"""The test of `short`, which imports the module inside its body."""

import importlib


def test_one_line_bodies():
    """Each one-line function the test calls returns its value.

    `never`, and the `inner` that `wrapped` defines, are not called: the import runs
    their `def` lines and nothing more.
    """
    short = importlib.import_module("basic.short")
    assert short.one() == 3
    assert short.Box().get() == 4
    assert short.wrapped() == "wrapped"
