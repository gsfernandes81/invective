"""Child 0: the selection run once under the tracer, and the map it writes.

Child 0 is the green-baseline check and the pre-scan in one run. It runs the whole
selection with the tracer resumed, then writes the map to `map.json` in the run's
directory: per test, the lines of each target it ran and its wall and CPU seconds by
phase. A line counts for a test when it ran in the test's setup, call or teardown,
or while a fixture the test used was being set up, whichever test that set-up ran
for. The engine reads the map back to choose each mutant's tests, to run them
cheapest first, and to give each child its wall budget.
"""

from __future__ import annotations

import json
import os
import resource
import time
from collections.abc import Generator
from typing import TYPE_CHECKING

import pytest

from invective import children, protocol, switch, tracer
from invective.errors import Refusal

if TYPE_CHECKING:
    from invective.engine import Context

#: The phases of a test, in the order they run.
PHASES = ("setup", "call", "teardown")

#: The file in the run's directory that child 0's stdout and stderr go to.
OUT = "baseline.out"


def run(context: Context) -> tuple[dict, dict[str, dict]]:
    """Run child 0 and return what it wrote; refuses the run if it did not finish.

    The first value is the map as `map.json` holds it, with the baseline's report
    events added under `reports`. The second is the map's `tests`, keyed by node id,
    each given an `outcome` of `passed` or `failed`.
    """
    session, state, rundir = context
    config = session.config
    events = os.path.join(rundir, "baseline.jsonl")
    out = os.path.join(rundir, OUT)
    mapfile = os.path.join(rundir, "map.json")

    def child() -> None:
        """Run the selection under the tracer and write the events file and the map."""
        protocol.recapture(config, out)
        state.tracer.resume()
        with protocol.Recorder(events, full=True) as recorder:
            prescan = _Prescan(state.tracer)
            config.pluginmanager.register(recorder, "invective-recorder")
            config.pluginmanager.register(prescan, "invective-prescan")
            protocol.run_items(session.items, lambda: False)
            protocol.teardown(session, recorder)
            state.tracer.pause()

            tests = {item.nodeid: _entry(item.nodeid, prescan) for item in session.items}
            with open(mapfile, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "tests": tests,
                        "import": state.tracer.lines_of(tracer.IMPORT),
                        "files": sorted(state.tracer.files),
                    },
                    fh,
                )
            recorder.write({"event": "done"})

    result = children.run_one(children.Job("baseline", child, out, None))
    lines = protocol.read_events(events)
    if not lines or lines[-1].get("event") != "done":
        raise Refusal(
            f"the baseline run {children.describe_status(result.status)} without "
            f"finishing; its output ends:\n{children.tail(out)}"
        )

    with open(mapfile, encoding="utf-8") as fh:
        data = json.load(fh)
    tests = data["tests"]
    reports = [line for line in lines if line["event"] == "report"]
    for nodeid in tests:
        tests[nodeid]["outcome"] = "passed"
    for line in reports:
        test = tests.get(line["nodeid"])
        if test is not None and line["outcome"] == "failed":
            test["outcome"] = "failed"
    data["reports"] = reports
    return data, tests


def wall(test: dict) -> float:
    """Return a test's baseline wall seconds, its three phases summed."""
    return sum(test["wall"].values())


def _entry(nodeid: str, prescan: _Prescan) -> dict[str, object]:
    """Return the map's entry for the test *nodeid*, read after the run.

    The entry holds the lines of each target the test ran, in its own phases and
    in the set-up of every fixture it used, and its wall and CPU seconds by phase,
    0.0 for a phase that did not run.
    """
    keys: list[tuple[object, object]] = [(nodeid, phase) for phase in PHASES]
    keys += [("fixture", fid) for fid in prescan.fixtures.get(nodeid, [])]

    lines: dict[str, set[int]] = {}
    for key in keys:
        for path, ran in prescan.tracer.lines.get(key, {}).items():
            lines.setdefault(path, set()).update(ran)

    unrun = dict.fromkeys(PHASES, 0.0)
    return {
        "lines": {path: sorted(numbers) for path, numbers in lines.items()},
        "wall": unrun | prescan.wall.get(nodeid, {}),
        "cpu": unrun | prescan.cpu.get(nodeid, {}),
    }


@switch.harness
class _Prescan:
    """A pytest plugin, registered in child 0, that feeds the tracer its context.

    While a test's phase runs, the tracer's context is the test and the phase; while
    a fixture is set up, it is that fixture definition. The plugin keeps each test's
    fixtures and its wall and CPU seconds by phase.
    """

    def __init__(self, tracer_: tracer.Tracer) -> None:
        """Feed *tracer_*, which records the lines under each context."""
        self.tracer = tracer_
        #: A test's node id to the ids of the fixture definitions it used.
        self.fixtures: dict[str, list[int]] = {}
        #: A test's node id to its wall seconds by phase.
        self.wall: dict[str, dict[str, float]] = {}
        #: A test's node id to its CPU seconds by phase.
        self.cpu: dict[str, dict[str, float]] = {}

    def _phase(self, item: pytest.Item, phase: str) -> Generator[None, object, object]:
        """Wrap one phase of *item*: the tracer's context, and the phase's times."""
        self.tracer.context = (item.nodeid, phase)
        wall, cpu = time.perf_counter(), _cpu()
        try:
            return (yield)
        finally:
            self.cpu.setdefault(item.nodeid, {})[phase] = _cpu() - cpu
            self.wall.setdefault(item.nodeid, {})[phase] = time.perf_counter() - wall
            self.tracer.context = None

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> Generator[None, object, None]:
        """Attribute the setup phase of *item*."""
        yield from self._phase(item, "setup")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, None]:
        """Attribute the call phase of *item*."""
        yield from self._phase(item, "call")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> Generator[None, object, None]:
        """Attribute the teardown phase of *item*, then note the fixtures it used."""
        try:
            yield from self._phase(item, "teardown")
        finally:
            self.fixtures[item.nodeid] = _fixtures_used(item)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(
        self, fixturedef: pytest.FixtureDef[object]
    ) -> Generator[None, object, object]:
        """Attribute the set-up of *fixturedef*, then restore the context before it."""
        before = self.tracer.context
        self.tracer.context = ("fixture", id(fixturedef))
        try:
            return (yield)
        finally:
            self.tracer.context = before


def _fixtures_used(item: pytest.Item) -> list[int]:
    """Return the ids of the fixture definitions *item* used, sorted.

    Read at the end of the item's teardown, they are the definitions set up for it,
    those its body asked for by name, and every definition each of them overrides,
    which an overriding fixture may have run by asking for its own name.
    """
    request = getattr(item, "_request", None)
    active = getattr(request, "_fixture_defs", None) or {}
    chains = getattr(getattr(item, "_fixtureinfo", None), "name2fixturedefs", {})
    found = {id(fixturedef) for fixturedef in active.values()}
    for name in active:
        found.update(id(fixturedef) for fixturedef in chains.get(name, ()))
    return sorted(found)


@switch.harness
def _cpu() -> float:
    """Return the CPU seconds of this process and the children it has waited for."""
    # Not `os.times`, whose 10 ms ticks read a whole test as no time at all.
    own = resource.getrusage(resource.RUSAGE_SELF)
    waited = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + waited.ru_utime + waited.ru_stime
