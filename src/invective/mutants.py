"""The canary child, one child per mutant, and the verdict each child writes.

A child makes its mutant live before any test runs, in the target's modules already
imported and in any it imports, so a target that only a test imports is measured like
any other. It runs the tests that cover the mutant, cheapest first, stops at the
first failure, and writes its verdict as a line of its events file before the final
teardown; the engine reads the line after the child has ended. The canary child is
the same child with the canary live in every target. A test must fail in it with
`switch.Canary`, or the engine refuses the run: that failure is the evidence that the
selection runs the instrumented code.
"""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING, NamedTuple

import pytest

from invective import baseline, children, protocol, switch, tracer
from invective.errors import Refusal
from invective.instrument import Instrumented, Mutant

if TYPE_CHECKING:
    from invective.engine import Context, State

#: A child's wall budget is this many times the baseline wall seconds of the tests it
#: runs, and at least `FLOOR` seconds, since a slow machine has a slow baseline. A
#: child over its budget is killed and its mutant reported unmeasured.
MULTIPLIER = 10.0
FLOOR = 2.0


class Verdict(NamedTuple):
    """What the engine found for one mutant."""

    mutant: Mutant
    verdict: str  #: killed, survived, unreached or unmeasured
    killer: str | None  #: the node id of the first failing test
    when: str | None  #: the phase it failed in
    tests: int  #: how many tests cover the mutant
    note: str  #: why, for a verdict that is not a kill


class _Child(NamedTuple):
    """What one child runs: the mutant live in it, its tests, and its two files."""

    path: str | None  #: the target the mutant is in, or None for every target
    ordinal: int
    identity: str  #: the value of `environment.MUTANT` in the child
    items: list[pytest.Item]
    events: str  #: the child's events file
    out: str  #: the file the child's stdout and stderr go to


class _Pending(NamedTuple):
    """A mutant whose child the scheduler runs, as the job's key."""

    mutant: Mutant
    tests: int  #: how many tests cover the mutant
    child: _Child
    budget: float  #: the child's wall budget in seconds


def canary(context: Context, items: list[pytest.Item]) -> str:
    """Run the canary child over *items* and return the node id of its killer.

    Refuses the run when no test failed, or when the first failure's exception was
    not `switch.Canary`.
    """
    events = os.path.join(context.rundir, "canary.jsonl")
    out = os.path.join(context.rundir, "canary.out")
    child = _Child(None, switch.CANARY, "canary", items, events, out)
    children.run_one(
        children.Job("canary", functools.partial(_child_run, context, child), out, None)
    )

    verdict = _verdict_line(events)
    if verdict is None or verdict.get("verdict") != "killed":
        raise Refusal(
            "the forced-fail canary survived: a mutant that raises in every "
            f"instrumented function failed no test of the {len(items)} that ran the "
            "targets, so the targets' code is not what the tests run"
        )
    if not verdict.get("canary"):
        raise Refusal(
            "the forced-fail canary was live, but the failure was not the "
            f"canary's: {verdict['killer']} ({verdict['when']}) failed for a reason "
            "of its own, which proves nothing about whether the tests run the "
            "targets' code; it may depend on a test that does not run the targets"
        )
    return verdict["killer"]


def run(
    context: Context, tables: dict[str, Instrumented], tests: dict, workers: int
) -> list[Verdict]:
    """Return every mutant's verdict, in the order of the targets' tables.

    *tables* is each target's mutant table and *tests* the baseline's map of tests.
    A child runs for each mutant that a test covers, *workers* at a time. A mutant no
    test covers is unreached without a child, and one whose function ran at import is
    unmeasured.
    """
    session, state, rundir = context
    order = sorted(tests, key=lambda nodeid: baseline.wall(tests[nodeid]))
    items = {item.nodeid: item for item in session.items}
    # Matched by the function's own code object, not by its lines: a one-line
    # function's `def` line is its body's line too, and the module runs that line.
    ran_at_import = state.tracer.functions_of(tracer.IMPORT)

    verdicts: dict[str, Verdict] = {}
    jobs: list[children.Job[_Pending]] = []
    for path in sorted(state.targets):
        at_import = ran_at_import.get(path, set())
        for mutant in tables[path].mutants:
            covering = _covering(mutant, path, tests, order)
            if mutant.qualname in at_import:
                verdicts[mutant.id] = Verdict(
                    mutant,
                    "unmeasured",
                    killer=None,
                    when=None,
                    tests=len(covering),
                    note="the function ran at import, before any switch; the slow "
                    "path is not built yet",
                )
            elif not covering:
                verdicts[mutant.id] = Verdict(
                    mutant,
                    "unreached",
                    killer=None,
                    when=None,
                    tests=0,
                    note="no test ran the function in the baseline",
                )
            else:
                number = len(jobs)
                child = _Child(
                    path,
                    mutant.ordinal,
                    mutant.id,
                    [items[nodeid] for nodeid in covering],
                    os.path.join(rundir, f"{number}.jsonl"),
                    os.path.join(rundir, f"{number}.out"),
                )
                seconds = sum(baseline.wall(tests[nodeid]) for nodeid in covering)
                budget = max(FLOOR, MULTIPLIER * seconds)
                pending = _Pending(mutant, len(covering), child, budget)
                run_child = functools.partial(_child_run, context, child)
                jobs.append(children.Job(pending, run_child, child.out, budget))

    def done(result: children.Done[_Pending]) -> None:
        """Record the verdict of the mutant whose child has ended."""
        verdicts[result.key.mutant.id] = _read_verdict(result)

    children.run_all(jobs, workers, done)
    ordered = [
        mutant for path in sorted(state.targets) for mutant in tables[path].mutants
    ]
    return [verdicts[mutant.id] for mutant in ordered]


def _read_verdict(result: children.Done[_Pending]) -> Verdict:
    """Return the verdict of a mutant whose child has ended, from its events file.

    A child that wrote no verdict is unmeasured, with a note naming whether it ran
    over its budget or how it ended, with the end of its output.
    """
    mutant, tests, child, budget = result.key
    line = _verdict_line(child.events)
    if line is not None:
        return Verdict(
            mutant,
            line["verdict"],
            killer=line.get("killer"),
            when=line.get("when"),
            tests=tests,
            note="",
        )
    if result.timed_out:
        return Verdict(
            mutant,
            "unmeasured",
            killer=None,
            when=None,
            tests=tests,
            note=f"over its wall budget of {budget:.1f} s, unconfirmed",
        )
    return Verdict(
        mutant,
        "unmeasured",
        killer=None,
        when=None,
        tests=tests,
        note=f"the child {children.describe_status(result.status)} without a "
        f"verdict; its output ends:\n{children.tail(child.out, 10)}",
    )


def _activate(state: State, path: str | None, ordinal: int, identity: str) -> None:
    """Make *ordinal* live in the target *path* of this process.

    Every other target keeps running its original code. With no *path*, *ordinal* is
    live in every target, which is how the canary runs. An ordinal numbers a mutant
    within its own module's table.
    """
    for each in sorted(state.targets) if path is None else [path]:
        switch.activate(each, state.finder.modules.get(each, []), ordinal, identity)


def _child_run(context: Context, child: _Child) -> None:
    """Run in a forked child: the child's tests with its mutant live.

    The run stops at the first failure. The verdict, `survived` or `killed` with the
    killer, its phase and whether it failed with `switch.Canary`, is written to the
    events file before the final teardown, so a finalizer that raises cannot lose it.
    """
    session, state, _rundir = context
    config = session.config
    protocol.recapture(config, child.out)
    _activate(state, child.path, child.ordinal, child.identity)
    with protocol.Recorder(child.events, full=False) as recorder:
        config.pluginmanager.register(recorder, "invective-recorder")
        protocol.run_items(child.items, lambda: recorder.failed is not None)

        if recorder.failed is None:
            recorder.write({"verdict": "survived"})
        else:
            recorder.write(
                {
                    "verdict": "killed",
                    "killer": recorder.failed.nodeid,
                    "when": recorder.failed.when,
                    "canary": recorder.failed.canary,
                }
            )
        protocol.teardown(session, recorder)


def _verdict_line(events: str) -> dict | None:
    """Return the verdict line of the events file *events*, or None if it has none."""
    return next(
        (line for line in protocol.read_events(events) if "verdict" in line), None
    )


def _covering(mutant: Mutant, path: str, tests: dict, order: list[str]) -> list[str]:
    """Return the tests that ran a line of *mutant*'s span, in the order of *order*.

    *path* is the target's real path, which keys each test's lines in the map.
    """
    first, last = mutant.span
    found = []
    for nodeid in order:
        lines = tests[nodeid]["lines"].get(path)
        if lines and any(first <= line <= last for line in lines):
            found.append(nodeid)
    return found
