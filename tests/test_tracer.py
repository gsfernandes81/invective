"""The tracer: lines per context, nothing outside the targets after
warm-up, and nothing at all while paused."""

from __future__ import annotations

import sys

import pytest

from invective import tracer


@pytest.fixture
def traced(tmp_path):
    target = tmp_path / "target.py"
    target.write_text("def loop(n):\n    total = 0\n    for i in range(n):\n"
                      "        total += i\n    return total\n")
    other = tmp_path / "other.py"
    other.write_text("def helper():\n    return 1\n")
    namespace = {}
    for path in (target, other):
        exec(compile(path.read_text(), str(path), "exec"), namespace)
    t = tracer.Tracer({str(target.resolve())})
    t.start()
    try:
        yield t, namespace, str(target.resolve()), str(other)
    finally:
        sys.monitoring.set_events(t.tool, 0)
        for code in t.traced:
            sys.monitoring.set_local_events(t.tool, code, 0)
        sys.monitoring.free_tool_id(t.tool)


def test_lines_are_attributed_to_the_context_they_ran_under(traced):
    t, ns, target, _other = traced
    t.context = "a"
    ns["loop"](3)
    t.context = "b"
    ns["helper"]()
    # The `def` line ran when the module did, not on the call.
    assert t.lines_of("a") == {target: [2, 3, 4, 5]}
    assert t.lines_of("b") == {}


def test_every_file_that_ran_is_known_but_only_targets_have_lines(traced):
    t, ns, target, other = traced
    t.context = "a"
    ns["loop"](1)
    ns["helper"]()
    assert {target, other} <= t.files
    assert other not in t.lines_of("a")


def test_a_paused_tracer_records_nothing_and_a_resumed_one_records_again(traced):
    t, ns, target, _other = traced
    t.context = "warm"
    ns["loop"](1)
    t.pause()
    t.context = "paused"
    ns["loop"](1)
    ns["helper"]()
    assert t.lines_of("paused") == {}
    t.resume()
    t.context = "resumed"
    ns["loop"](1)
    assert t.lines_of("resumed") == {target: [2, 3, 4, 5]}


def test_the_start_event_fires_once_per_code_object(traced):
    """After its first call a function outside the targets costs nothing:
    its start event is disabled, so the callback is never entered again."""
    t, ns, _target, _other = traced
    t.context = "a"
    ns["helper"]()
    calls = []
    original = t._on_start
    t._on_start = lambda code, offset: calls.append(code) or original(code, offset)
    sys.monitoring.register_callback(t.tool, sys.monitoring.events.PY_START, t._on_start)
    ns["helper"]()
    ns["helper"]()
    assert calls == []


NESTED = '''\
def one() -> int: return 3


class C:
    def m(self) -> str:
        def inner() -> int:
            return 1
        return str(inner())


def outer() -> bool:
    class Local:
        def get(self) -> bool:
            return True
    return Local().get()
'''


def test_the_functions_that_ran_are_named_as_instrument_names_their_mutants(tmp_path):
    """Module-level code runs every `def` line, a one-line function's body
    line among them, but only a function called in a context is named in
    it; and the names are the qualnames of the mutants' table."""
    from invective import instrument
    path = tmp_path / "nested.py"
    path.write_text(NESTED)
    target = str(path.resolve())
    code, info = instrument.instrument(NESTED, target, "nested")
    t = tracer.Tracer({target})
    t.start()
    try:
        t.context = "import"
        namespace = {"__name__": "nested", "__file__": target}
        exec(code, namespace)
        t.context = "call"
        namespace["one"]()
        namespace["C"]().m()
        namespace["outer"]()
    finally:
        sys.monitoring.set_events(t.tool, 0)
        for each in t.traced:
            sys.monitoring.set_local_events(t.tool, each, 0)
        sys.monitoring.free_tool_id(t.tool)
    assert t.functions_of("import") == {target: {"<module>", "C"}}
    qualnames = {m.qualname for m in info.mutants}
    assert qualnames == {"one", "C.m", "C.m.<locals>.inner", "outer",
                         "outer.<locals>.Local.get"}
    assert qualnames <= t.functions_of("call")[target]


def test_the_tracer_takes_the_highest_free_tool_id():
    """`COVERAGE_ID` and the low ids stay free for a coverage.py started
    later in the session."""
    free = [tool for tool in range(6) if sys.monitoring.get_tool(tool) is None]
    t = tracer.Tracer(set())
    t.start()
    try:
        assert t.tool == max(free)
    finally:
        sys.monitoring.set_events(t.tool, 0)
        sys.monitoring.free_tool_id(t.tool)
