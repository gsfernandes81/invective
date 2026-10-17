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
