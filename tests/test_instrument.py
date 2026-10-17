"""The instrumented module: the switch, the guard and what it leaves alone."""

from __future__ import annotations

import ast
import dis

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
def module():
    code, info = instrument.instrument(SOURCE, "/nowhere/mod.py", "mod")
    namespace = {"__name__": "mod", "__file__": "/nowhere/mod.py"}
    exec(code, namespace)
    return code, info, namespace


def test_ids_are_structural_and_in_table_order(module):
    _code, info, _ns = module
    assert [m.id for m in info.mutants] == [
        "mod:plain:body:0", "mod:plain:body:1", "mod:plain:body:-1",
        "mod:Thing.flag.<locals>.inner:body:''", "mod:Thing.flag.<locals>.inner:body:'xyzzy'",
        "mod:Thing.flag:body:True", "mod:Thing.flag:body:False",
        "mod:gen:body:iter([])", "mod:gen:body:iter([0])", "mod:gen:body:iter([1])",
        "mod:gen:body:iter([-1])"]
    assert [m.ordinal for m in info.mutants] == list(range(1, 12))


def test_a_constructor_an_empty_body_and_the_main_guard_have_no_mutant(module):
    _code, info, ns = module
    assert not any("__init__" in m.id or "nothing" in m.id or "main" in m.id
                   for m in info.mutants)
    assert "main" not in ns


def test_the_module_runs_its_original_code_with_no_mutant_live(module):
    _code, _info, ns = module
    assert ns["__invective__"] is None
    assert ns["plain"](1) == 2
    assert ns["Thing"]().flag() is True
    assert list(ns["gen"]()) == [1]
    assert ns["plain"].__doc__ == "Documented."
    assert ns["__doc__"] == "The module's docstring."


def test_each_ordinal_switches_its_own_function_only(module):
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
    _code, _info, ns = module
    ns["__invective__"] = switch.CANARY
    for call in (lambda: ns["plain"](1), lambda: ns["Thing"](), lambda: ns["nothing"](),
                 lambda: list(ns["gen"]())):
        with pytest.raises(switch.Canary):
            call()


def test_the_canary_is_not_an_exception_a_test_can_swallow():
    assert not issubclass(switch.Canary, Exception)


def _instructions(function):
    """The instructions that do something, in order: the no-ops a compiler
    adds (`RESUME`, 3.14's `NOT_TAKEN`) left out."""
    return [i for i in dis.get_instructions(function)
            if i.opname not in ("RESUME", "NOT_TAKEN", "NOP")]


def test_the_guard_is_one_global_read_and_one_compare(module):
    """With nothing live, a function does two things before its own body:
    load the global and jump on None, straight to the body. Anything more is
    paid on every call of every function of every mutant's run."""
    _code, _info, ns = module
    ops = _instructions(ns["plain"])
    assert (ops[0].opname, ops[0].argval) == ("LOAD_GLOBAL", "__invective__"), ops[:4]
    assert ops[1].opname == "POP_JUMP_IF_NONE", ops[:4]
    landing = next(i for i in ops if i.offset == ops[1].argval)
    assert landing.opname.startswith("LOAD_FAST") and landing.argval == "x", landing


def test_line_numbers_and_the_path_are_the_files_own(module):
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
    _code, info, _ns = module
    plain = info.mutants[0]
    assert (plain.line, plain.span) == (10, (10, 10))
    flag = next(m for m in info.mutants if m.id == "mod:Thing.flag:body:True")
    assert (flag.line, flag.span) == (18, (18, 20))


def test_the_prologue_follows_the_docstring_and_future_imports():
    tree = ast.parse(SOURCE)
    code, _info = instrument.instrument(SOURCE, "/nowhere/mod.py", "mod")
    # A `from __future__` after any other statement is a SyntaxError, so
    # compiling at all shows the order held; the docstring check shows the
    # prologue did not become the module's docstring.
    ns = {"__name__": "mod", "__file__": "/nowhere/mod.py"}
    exec(code, ns)
    assert ns["__doc__"] == tree.body[0].value.value
