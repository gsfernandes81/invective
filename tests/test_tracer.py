"""The tracer records lines per context, and only the targets' lines.

After warm-up nothing outside the targets fires an event, and while the tracer is
paused nothing does.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Generator
from typing import Any

import pytest

from invective import instrument, tracer


def _release(t: tracer.Tracer) -> None:
    """Turn every event of *t* off and free its tool id, for the next test."""
    tool = t._tool()
    sys.monitoring.set_events(tool, 0)
    for code in t.traced:
        sys.monitoring.set_local_events(tool, code, 0)
    sys.monitoring.free_tool_id(tool)


@pytest.fixture
def traced(
    tmp_path: pathlib.Path,
) -> Generator[tuple[tracer.Tracer, dict[str, Any], str, str]]:
    """Yield a started tracer, the namespace of two modules, and their two paths.

    The first module is the tracer's target and the second is not.
    """
    target = tmp_path / "target.py"
    target.write_text(
        "def loop(n):\n    total = 0\n    for i in range(n):\n"
        "        total += i\n    return total\n"
    )
    other = tmp_path / "other.py"
    other.write_text("def helper():\n    return 1\n")
    namespace: dict[str, Any] = {}
    for path in (target, other):
        exec(compile(path.read_text(), str(path), "exec"), namespace)
    t = tracer.Tracer({str(target.resolve())})
    t.start()
    try:
        yield t, namespace, str(target.resolve()), str(other)
    finally:
        _release(t)


def test_lines_are_attributed_to_the_context_they_ran_under(traced):
    """A target's lines are recorded under the context set when they ran."""
    t, ns, target, _other = traced
    t.context = "a"
    ns["loop"](3)
    t.context = "b"
    ns["helper"]()
    # The `def` line ran when the module did, not on the call.
    assert t.lines_of("a") == {target: [2, 3, 4, 5]}
    assert t.lines_of("b") == {}


def test_every_file_that_ran_is_known_but_only_targets_have_lines(traced):
    """The tracer records every file a code object ran from, but only targets' lines."""
    t, ns, target, other = traced
    t.context = "a"
    ns["loop"](1)
    ns["helper"]()
    assert {target, other} <= t.files
    assert other not in t.lines_of("a")


def test_a_paused_tracer_records_nothing_and_a_resumed_one_records_again(traced):
    """A paused tracer records no line, and after `resume` it records again."""
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
    """After its first call, a function outside the targets costs nothing.

    Its start event is disabled, so the callback is not entered again.
    """
    t, ns, _target, _other = traced
    t.context = "a"
    ns["helper"]()
    calls = []
    original = t._on_start
    t._on_start = lambda code, offset: calls.append(code) or original(code, offset)
    sys.monitoring.register_callback(
        t._tool(), sys.monitoring.events.PY_START, t._on_start
    )
    ns["helper"]()
    ns["helper"]()
    assert calls == []


NESTED = """\
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
"""


def test_the_functions_that_ran_are_named_as_instrument_names_their_mutants(
    tmp_path: pathlib.Path,
) -> None:
    """Only a function called under a context is named in it, by its mutants' qualname.

    Module-level code runs every `def` line, a one-line function's body line among
    them, and that does not name the function.
    """
    path = tmp_path / "nested.py"
    path.write_text(NESTED)
    target = str(path.resolve())
    code, info = instrument.instrument(NESTED, target, "nested")
    t = tracer.Tracer({target})
    t.start()
    try:
        t.context = "import"
        namespace: dict[str, Any] = {"__name__": "nested", "__file__": target}
        exec(code, namespace)
        t.context = "call"
        namespace["one"]()
        namespace["C"]().m()
        namespace["outer"]()
    finally:
        _release(t)

    assert t.functions_of("import") == {target: {"<module>", "C"}}
    qualnames = {m.qualname for m in info.mutants}
    assert qualnames == {
        "one",
        "C.m",
        "C.m.<locals>.inner",
        "outer",
        "outer.<locals>.Local.get",
    }
    assert qualnames <= t.functions_of("call")[target]


def test_the_tracer_takes_the_highest_free_tool_id() -> None:
    """The tracer leaves `COVERAGE_ID` and the low ids free for a later coverage.py."""
    free = [tool for tool in range(6) if sys.monitoring.get_tool(tool) is None]
    t = tracer.Tracer(set())
    t.start()
    try:
        assert t.tool == max(free)
    finally:
        _release(t)


def test_code_marked_harness_records_no_line(tmp_path: pathlib.Path) -> None:
    """A target's code marked `switch.harness`, nested code and methods too, is untraced.

    Child 0 runs some of the harness inside a test's phase, and a line it recorded
    there would be credited to the test.
    """
    target = tmp_path / "marked.py"
    target.write_text(
        "from invective import switch\n\n\n"
        "@switch.harness\ndef marked():\n    return (lambda: 1)()\n\n\n"
        "@switch.harness\nclass Kept:\n    def method(self):\n        return 2\n\n\n"
        "def plain():\n    return 3\n"
    )
    namespace: dict[str, Any] = {}
    exec(compile(target.read_text(), str(target), "exec"), namespace)
    t = tracer.Tracer({str(target.resolve())})
    t.start()
    try:
        t.context = "a"
        namespace["marked"]()
        namespace["Kept"]().method()
        namespace["plain"]()
        assert t.lines_of("a") == {str(target.resolve()): [16]}
    finally:
        _release(t)
