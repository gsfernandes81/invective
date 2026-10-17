"""The instrumented module: the switch, the guard, and what it leaves alone."""

from __future__ import annotations

import ast
import dis
import types
from collections.abc import Callable
from typing import Any

import pytest

from invective import instrument, switch

SOURCE = '''"""The module's docstring."""
from __future__ import annotations

import os


def plain(x) -> int:
    """Documented."""
    global os
    return x + 1


class Thing:
    def __init__(self):
        self.n = 0

    def flag(self) -> bool:
        def inner() -> str:
            return "a"
        return bool(inner())


def gen() -> Iterator[int]:
    yield 1


def nothing():
    pass


if __name__ == "__main__":
    def main() -> int:
        return 3
'''


@pytest.fixture
def module() -> tuple[types.CodeType, instrument.Instrumented, dict]:
    """Return `SOURCE` instrumented and run: its code, its table and its namespace."""
    code, info = instrument.instrument(SOURCE, "/nowhere/mod.py", "mod")
    namespace = {"__name__": "mod", "__file__": "/nowhere/mod.py"}
    exec(code, namespace)
    return code, info, namespace


def test_ids_are_structural_and_in_table_order(module):
    """A mutant's id names its module, function and value; ordinals count from 1."""
    _code, info, _ns = module
    assert [m.id for m in info.mutants] == [
        "mod:plain:body:0",
        "mod:plain:body:1",
        "mod:plain:body:-1",
        "mod:Thing.flag.<locals>.inner:body:''",
        "mod:Thing.flag.<locals>.inner:body:'xyzzy'",
        "mod:Thing.flag:body:True",
        "mod:Thing.flag:body:False",
        "mod:gen:body:iter([])",
        "mod:gen:body:iter([0])",
        "mod:gen:body:iter([1])",
        "mod:gen:body:iter([-1])",
    ]
    assert [m.ordinal for m in info.mutants] == list(range(1, 12))


def test_a_constructor_an_empty_body_and_the_main_guard_have_no_mutant(module):
    """A constructor, an empty body and the code under the main guard get no mutant."""
    _code, info, ns = module
    assert not any(
        "__init__" in m.id or "nothing" in m.id or "main" in m.id for m in info.mutants
    )
    assert "main" not in ns


def test_the_module_runs_its_original_code_with_no_mutant_live(module):
    """With no mutant live, every function and docstring is the source's own."""
    _code, _info, ns = module
    assert ns["__invective__"] is None
    assert ns["plain"](1) == 2
    assert ns["Thing"]().flag() is True
    assert list(ns["gen"]()) == [1]
    assert ns["plain"].__doc__ == "Documented."
    assert ns["__doc__"] == "The module's docstring."


def test_each_ordinal_switches_its_own_function_only(module):
    """Setting the global to a mutant's ordinal changes that function and no other."""
    _code, info, ns = module
    by_id = {m.id: m.ordinal for m in info.mutants}
    ns["__invective__"] = by_id["mod:plain:body:-1"]
    assert ns["plain"](1) == -1
    assert ns["Thing"]().flag() is True
    ns["__invective__"] = by_id["mod:gen:body:iter([])"]
    assert list(ns["gen"]()) == []
    assert ns["plain"](1) == 2
    ns["__invective__"] = by_id["mod:Thing.flag.<locals>.inner:body:''"]
    assert ns["Thing"]().flag() is False


def test_the_canary_raises_in_every_function_with_a_body(module):
    """With the canary live, every function raises `Canary`, `__init__` and `pass` too."""
    _code, _info, ns = module
    ns["__invective__"] = switch.CANARY
    for call in (
        lambda: ns["plain"](1),
        ns["Thing"],
        ns["nothing"],
        lambda: list(ns["gen"]()),
    ):
        with pytest.raises(switch.Canary):
            call()


def test_the_canary_is_not_an_exception_a_test_can_swallow():
    """`Canary` is outside `Exception`, so a test's `except Exception` cannot catch it."""
    assert not issubclass(switch.Canary, Exception)


def _instructions(function: Callable[..., object]) -> list[dis.Instruction]:
    """Return the instructions of *function* that do something, in order.

    The no-ops a compiler adds (`RESUME`, 3.14's `NOT_TAKEN`) are left out.
    """
    return [
        i
        for i in dis.get_instructions(function)
        if i.opname not in ("RESUME", "NOT_TAKEN", "NOP")
    ]


def test_the_guard_is_one_global_read_and_one_compare(module):
    """With nothing live, a function reads the global and jumps on None to its body.

    Anything more is paid on every call of every function of every mutant's run.
    """
    _code, _info, ns = module
    ops = _instructions(ns["plain"])
    assert (ops[0].opname, ops[0].argval) == ("LOAD_GLOBAL", "__invective__"), ops[:4]
    assert ops[1].opname == "POP_JUMP_IF_NONE", ops[:4]
    landing = next(i for i in ops if i.offset == ops[1].argval)
    assert landing.opname.startswith("LOAD_FAST"), landing
    assert landing.argval == "x", landing


def test_line_numbers_and_the_path_are_the_files_own(module):
    """The compiled code keeps the file's path and lines; the guard takes the body's."""
    code, _info, ns = module
    assert code.co_filename == "/nowhere/mod.py"
    assert ns["plain"].__code__.co_firstlineno == 7
    assert ns["Thing"].flag.__code__.co_firstlineno == 17
    # The guard and every branch sit on the first running statement's line,
    # after `global`, which has no instruction of its own.
    lines = {i.positions.lineno for i in dis.get_instructions(ns["plain"]) if i.positions}
    assert lines == {7, 10}
    ns["__invective__"] = switch.CANARY
    with pytest.raises(switch.Canary) as raised:
        ns["plain"](1)
    assert raised.traceback[-1].lineno + 1 == 10


def test_a_mutants_line_and_span_are_the_bodys(module):
    """A mutant's line is its body's first statement, and its span the whole body."""
    _code, info, _ns = module
    plain = info.mutants[0]
    assert (plain.line, plain.span) == (10, (10, 10))
    flag = next(m for m in info.mutants if m.id == "mod:Thing.flag:body:True")
    assert (flag.line, flag.span) == (18, (18, 20))


def test_the_prologue_follows_the_docstring_and_future_imports():
    """The prologue goes after the module docstring and the `from __future__` imports."""
    tree = ast.parse(SOURCE)
    code, _info = instrument.instrument(SOURCE, "/nowhere/mod.py", "mod")
    # A `from __future__` after any other statement is a SyntaxError, so
    # compiling at all shows the order held; the docstring check shows the
    # prologue did not become the module's docstring.
    ns = {"__name__": "mod", "__file__": "/nowhere/mod.py"}
    exec(code, ns)
    docstring = tree.body[0]
    assert isinstance(docstring, ast.Expr)
    assert isinstance(docstring.value, ast.Constant)
    assert ns["__doc__"] == docstring.value.value


MARKED = """from invective import switch

harness = switch.harness


@switch.harness
def marked() -> int:
    def nested() -> int:
        return 1
    return nested()


@switch.harness
class Kept:
    def method(self) -> bool:
        return True


@harness
def bare() -> int:
    return 2


def plain() -> int:
    return 3
"""


def test_code_marked_harness_has_no_guard_and_no_mutant():
    """A function or class marked `switch.harness` is left as written, nested code too.

    With the canary live, the marked code runs as written and the rest raises. Only
    the qualified spelling marks: a decorator named `harness` alone does not.
    """
    code, info = instrument.instrument(MARKED, "/nowhere/marked.py", "marked")
    ns: dict[str, Any] = {"__name__": "marked", "__file__": "/nowhere/marked.py"}
    exec(code, ns)
    assert [m.id for m in info.mutants] == [
        "marked:bare:body:0",
        "marked:bare:body:1",
        "marked:bare:body:-1",
        "marked:plain:body:0",
        "marked:plain:body:1",
        "marked:plain:body:-1",
    ]
    assert info.functions == 2
    for function in (ns["marked"], ns["Kept"].method):
        names = {i.argval for i in dis.get_instructions(function, show_caches=False)}
        assert "__invective__" not in names, function

    ns["__invective__"] = switch.CANARY
    assert ns["marked"]() == 1
    assert ns["Kept"]().method() is True
    for call in (ns["bare"], ns["plain"]):
        with pytest.raises(switch.Canary):
            call()
