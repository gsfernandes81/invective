"""The tests of `script`: one imports it, one runs it from its path as `__main__`."""

import os
import runpy

import pytest
import runpath
from runpath import script

SCRIPT = os.path.join(os.path.dirname(runpath.__file__), "script.py")


def test_name():
    """The imported module's name is "script"."""
    assert script.name() == "script"


def test_script_exit_code():
    """The script exits with 3."""
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(SCRIPT, run_name="__main__")
    assert exited.value.code == 3
