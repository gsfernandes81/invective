"""The tests of `plug`: one imports it, one loads it from its path."""

import importlib.util
import os

import bp
from bp import plug

PLUG = os.path.join(os.path.dirname(bp.__file__), "plug.py")


def test_name():
    """The imported module's name is "plug"."""
    assert plug.name() == "plug"


def test_plugin_loaded_by_path():
    """The module loaded from the file answers 42."""
    spec = importlib.util.spec_from_file_location("someplugin", PLUG)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.answer() == 42
