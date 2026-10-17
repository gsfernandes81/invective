import time

import basic.gate as gate
from basic import gate as g


def test_open_hours():
    assert gate.is_open(10)
    assert not gate.is_open(3)


def test_closed_at_night():
    """Fails with `is_open` replaced by `True`, as `test_open_hours` does,
    but costs more in the baseline, so it runs second and is not named."""
    time.sleep(0.05)
    assert not gate.is_open(22)


def test_count_is_not_one():
    assert gate.count(["a", "b"]) != 1


def test_first_even():
    assert next(gate.evens(10), 0) == 0


def test_counter():
    c = g.Counter()
    c.bump()
    assert c.n == 1
    assert c.value() < 2


def test_doubled(doubled):
    assert doubled == 42


def test_doubled_again(doubled):
    assert doubled + 1 == 43


def test_file_and_lines():
    """The instrumented module keeps the file's path and line numbers: a
    traceback through `bump` lands on the line the source file gives its
    body, read from the file by `ast` and not from the code object."""
    import ast
    import inspect
    import sys
    import traceback
    assert gate.__file__.endswith(os.path.join("src", "basic", "gate.py"))
    assert gate.is_open.__doc__.startswith("Killed")
    lines, start = inspect.getsourcelines(gate.is_open)
    assert "def is_open" in lines[0]
    with open(gate.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    counter = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Counter")
    bump = next(n for n in counter.body if isinstance(n, ast.FunctionDef) and n.name == "bump")
    body_line = next(n for n in bump.body if isinstance(n, ast.AugAssign)).lineno
    try:
        gate.Counter().bump.__func__(None)
    except AttributeError:
        frame = traceback.extract_tb(sys.exc_info()[2])[-1]
        assert frame.filename == gate.__file__
        assert frame.lineno == body_line, (frame.lineno, body_line)
    else:
        raise AssertionError("no error")


import os  # noqa: E402
