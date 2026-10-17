"""The tests of `gate`, which reach the module under two bindings.

`gate` and `basic.gate` are the same module object, so a mutant live in it is live
through both.
"""

import ast
import inspect
import os
import sys
import time
import traceback

import basic.gate
from basic import gate


def test_open_hours():
    """An hour in the day is open and one at night is not."""
    assert gate.is_open(10)
    assert not gate.is_open(3)


def test_closed_at_night():
    """A late hour is closed.

    It fails with `is_open` replaced by `True`, as `test_open_hours` does, but costs
    more in the baseline, so it runs second and is not named.
    """
    time.sleep(0.05)
    assert not gate.is_open(22)


def test_count_is_not_one():
    """Two items do not count as one."""
    assert gate.count(["a", "b"]) != 1


def test_first_even():
    """The first even number, or 0 for none, is 0."""
    assert next(gate.evens(10), 0) == 0


def test_counter():
    """One bump makes the count 1, and `value` reads under 2."""
    c = basic.gate.Counter()
    c.bump()
    assert c.n == 1
    assert c.value() < 2


def test_doubled(doubled):
    """The session fixture's result is 42."""
    assert doubled == 42


def test_doubled_again(doubled):
    """A second test uses the same session fixture's result."""
    assert doubled + 1 == 43


def test_file_and_lines():
    """The instrumented module keeps the file's path and line numbers.

    A traceback through `bump` lands on the line the source file gives its body,
    read from the file by `ast`, not from the code object.
    """
    assert gate.__file__.endswith(os.path.join("src", "basic", "gate.py"))
    doc = gate.is_open.__doc__
    assert doc is not None
    assert doc.startswith("Killed")
    lines, _start = inspect.getsourcelines(gate.is_open)
    assert "def is_open" in lines[0]
    with open(gate.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    counter = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Counter"
    )
    bump = next(
        n for n in counter.body if isinstance(n, ast.FunctionDef) and n.name == "bump"
    )
    body_line = next(n for n in bump.body if isinstance(n, ast.AugAssign)).lineno
    try:
        # `None` as `self` makes the body raise on its first line, the one checked.
        gate.Counter().bump.__func__(None)  # ty: ignore[invalid-argument-type]
    except AttributeError:
        frame = traceback.extract_tb(sys.exc_info()[2])[-1]
        assert frame.filename == gate.__file__
        assert frame.lineno == body_line, (frame.lineno, body_line)
    else:
        raise AssertionError("no error")
