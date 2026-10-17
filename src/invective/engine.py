"""The run: a parent that runs no test, child 0 for the baseline and the
map, the canary, then one forked child per mutant, and the verdicts read
back.

The parent is the pytest session the user started, after collection. It
runs no test and imports no target of its own accord: each target's mutant
table is built from its source, and a target is imported in the parent only
when a conftest or a test module imports it during collection. Every test
runs in a child forked from the parent. Child 0 runs the whole selection
once, which is the green-baseline check and the pre-scan in one run. The
canary child makes every instrumented function raise `switch.Canary` and
must see a test fail with it, or the run is refused: the proof that the
selection runs instrumented code. Then each mutant's covering tests run in
a child of their own, cheapest first, stopping at the first failure. The
child makes its mutant live before any test runs, for the modules already
imported and for any it imports, so a target that only a test imports is
measured like any other. The child writes its verdict as a line in a file,
which the parent reads after `waitpid`.
"""

from __future__ import annotations

import fnmatch
import gc
import json
import os
import resource
import shutil
import tempfile
import threading
import time
from typing import NamedTuple

import pytest

from invective import children, finder, switch, tracer
from invective.errors import Refusal
from invective.instrument import Instrumented, Mutant

#: Set in the environment, keeps the run's directory of child output and
#: verdict files, for reading a child's run after the fact.
KEEP = "INVECTIVE_KEEP"

#: A child's wall budget is this many times the baseline wall time of the
#: tests it runs, and at least `FLOOR` seconds: a slow machine has a slow
#: baseline. The confirmation of a timeout is stage 2's.
MULTIPLIER = 10.0
FLOOR = 2.0


class State:
    """What `pytest_load_initial_conftests` sets up: the targets, the finder
    that serves them, and the tracer that has run since."""

    def __init__(self, targets: set[str]):
        self.targets = targets
        self.tracer = tracer.Tracer(targets)
        self.tracer.start()
        self.finder = finder.install(targets)


def targets_of(paths: list[str], here: str, test_patterns: list[str]) -> set[str]:
    """The real paths of the files *paths* name, a directory standing for
    the `.py` files under it that are not a conftest or a test module."""
    found: set[str] = set()
    for given in paths:
        path = os.path.join(here, given)
        if os.path.isfile(path):
            found.add(finder.real(path))
        elif os.path.isdir(path):
            for top, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                                 and d != "__pycache__"
                                 and not os.path.exists(os.path.join(top, d, "pyvenv.cfg")))
                for name in sorted(files):
                    if (name.endswith(".py") and name != "conftest.py"
                            and not any(fnmatch.fnmatch(name, pat) for pat in test_patterns)):
                        found.add(finder.real(os.path.join(top, name)))
        else:
            raise pytest.UsageError("--mutate: no such file or directory: %s" % given)
    if not found:
        raise pytest.UsageError("--mutate: nothing to mutate under %s" % ", ".join(paths))
    return found


class Verdict(NamedTuple):
    mutant: Mutant
    verdict: str            #: killed, survived, unreached, unmeasured
    killer: str | None      #: the node id of the first failing test
    when: str | None        #: the phase it failed in
    tests: int              #: how many tests cover the mutant
    note: str               #: why, for a verdict that is not a kill


class Report(NamedTuple):
    targets: list[str]
    verdicts: list[Verdict]
    baseline: dict
    canary: str             #: the test the forced-fail canary failed
    seconds: float


def run(session, state: State, workers: int) -> Report:
    """The whole run, on the collected *session*; raises `Refusal`."""
    config = session.config
    started = time.monotonic()
    if session.testsfailed:
        raise Refusal("%d error(s) during collection, so the selection is not "
                      "the one asked for" % session.testsfailed)
    if not session.items:
        raise Refusal("pytest collected no tests, so there is nothing to notice a mutant")
    tables = _tables(state)
    _refuse_threads()
    gc.collect()
    gc.freeze()
    state.tracer.pause()
    rundir = tempfile.mkdtemp(prefix="invective-")
    try:
        baseline, tests = _baseline(session, state, rundir)
        _replay(session, baseline["reports"])
        # The replay runs the project's report hooks in the parent, which
        # may start a thread before the canary's fork.
        _refuse_threads()
        failed = [nodeid for nodeid, test in tests.items() if test["outcome"] == "failed"]
        if failed:
            shown = ", ".join(failed[:5]) + (" and %d more" % (len(failed) - 5)
                                             if len(failed) > 5 else "")
            output = children.tail(os.path.join(rundir, "baseline.out"))
            raise Refusal("the selection fails before any mutant: %s%s" % (
                shown, "; the run's output ends:\n" + output if output.strip() else ""))
        items = {item.nodeid: item for item in session.items}
        order = sorted(tests, key=lambda nodeid: _wall(tests[nodeid]))
        covering_any = [nodeid for nodeid in tests
                        if any(tests[nodeid]["lines"].get(path) for path in state.targets)]
        if not covering_any:
            raise Refusal("no test in the selection runs a line of the targets")
        canary = _canary(session, state, rundir, [items[n] for n in covering_any])
        verdicts = _mutants(session, state, tables, rundir, tests, order, items, workers)
    finally:
        if not os.environ.get(KEEP):
            shutil.rmtree(rundir, ignore_errors=True)
    return Report(sorted(state.targets), verdicts, baseline, canary,
                  time.monotonic() - started)


def _refuse_threads() -> None:
    alive = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
    if alive:
        raise Refusal("threads are alive in the parent, so a forked child could "
                      "deadlock: %s" % ", ".join(alive))


def _tables(state: State) -> dict[str, Instrumented]:
    """Every target's mutant table, built from its source in the parent
    without importing it; refuses a target that does not compile."""
    tables = {}
    for path in sorted(state.targets):
        try:
            tables[path] = finder.table(path)
        except (SyntaxError, ValueError, OSError) as exc:
            raise Refusal("%s cannot be read as Python: %s" % (path, exc)) from exc
    return tables


def _recapture(config, out: str) -> None:
    """pytest's global capture started again in the child, over the child's
    own output file.

    The parent's capture holds the terminal as the descriptor it restores
    between tests, and one temporary file every child would share. Stopping
    it puts the terminal back on stdout and stderr; the child's file goes
    over them; and a fresh capture saves that file as what it restores, with
    temporary files of the child's own.
    """
    capman = config.pluginmanager.getplugin("capturemanager")
    if capman is not None:
        capman.stop_global_capturing()
    fd = os.open(out, os.O_WRONLY | os.O_APPEND)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    if capman is not None:
        capman.start_global_capturing()
        capman.suspend_global_capture()


def _run_items(items, stop) -> None:
    """*items* through `pytest_runtest_protocol` in order, each told its
    successor so that fixtures of a wider scope live across them, stopping
    when *stop* returns true. What is still set up after a stop stays set
    up for `_teardown`."""
    for index, item in enumerate(items):
        nextitem = items[index + 1] if index + 1 < len(items) else None
        item.ihook.pytest_runtest_protocol(item=item, nextitem=nextitem)
        if stop():
            break


def _teardown(session, recorder: _Recorder) -> None:
    """Everything still set up is torn down; an exception from a finalizer
    is written as an event, whatever its class, and goes no further.

    A finalizer can raise `Canary`, a `pytest.fail` or `pytest.skip`
    outcome or `SystemExit`, none of them an `Exception`, so the caller
    writes its verdict before this runs.
    """
    try:
        session._setupstate.teardown_exact(None)
    except BaseException as exc:
        recorder.write({"event": "teardown-raised", "type": type(exc).__qualname__})


class _Failure(NamedTuple):
    nodeid: str
    when: str
    canary: bool            #: the failure's exception is `switch.Canary`


class _Recorder:
    """Registered in a child: each report as one line, and the first failure."""

    def __init__(self, path: str, full: bool):
        self.fh = open(path, "w", encoding="utf-8")
        self.full = full
        self.failed: _Failure | None = None
        self._canaries: set[tuple[str, str]] = set()

    def write(self, line: dict) -> None:
        self.fh.write(json.dumps(line) + "\n")
        self.fh.flush()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        if report.failed and call.excinfo is not None and call.excinfo.type is switch.Canary:
            self._canaries.add((report.nodeid, report.when))
        return report

    def pytest_runtest_logreport(self, report):
        line = {"event": "report", "nodeid": report.nodeid, "when": report.when,
                "outcome": report.outcome, "duration": report.duration}
        if self.full:
            line["json"] = report._to_json()
        self.write(line)
        if report.failed and self.failed is None:
            self.failed = _Failure(report.nodeid, report.when,
                                   (report.nodeid, report.when) in self._canaries)


class _Prescan:
    """Registered in child 0: sets the tracer's context to the test and phase
    running, or the fixture being set up, and keeps each test's fixtures and
    its wall and CPU seconds by phase."""

    def __init__(self, tracer_):
        self.tracer = tracer_
        self.fixtures: dict[str, list[int]] = {}
        self.wall: dict[str, dict[str, float]] = {}
        self.cpu: dict[str, dict[str, float]] = {}

    def _phase(self, item, phase):
        self.tracer.context = (item.nodeid, phase)
        wall, cpu = time.perf_counter(), _cpu()
        try:
            return (yield)
        finally:
            self.cpu.setdefault(item.nodeid, {})[phase] = _cpu() - cpu
            self.wall.setdefault(item.nodeid, {})[phase] = time.perf_counter() - wall
            self.tracer.context = None

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item):
        yield from self._phase(item, "setup")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        yield from self._phase(item, "call")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item):
        try:
            yield from self._phase(item, "teardown")
        finally:
            self.fixtures[item.nodeid] = _fixtures_used(item)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        before = self.tracer.context
        self.tracer.context = ("fixture", id(fixturedef))
        try:
            return (yield)
        finally:
            self.tracer.context = before


def _fixtures_used(item) -> list[int]:
    """The ids of the fixture definitions *item* used, read at the end of its
    teardown: those set up for it, those its body asked for by name, and
    every definition each of them overrides, which an overriding fixture
    may have run by asking for its own name."""
    request = getattr(item, "_request", None)
    active = getattr(request, "_fixture_defs", None) or {}
    chains = getattr(getattr(item, "_fixtureinfo", None), "name2fixturedefs", {})
    found = {id(fixturedef) for fixturedef in active.values()}
    for name in active:
        found.update(id(fixturedef) for fixturedef in chains.get(name, ()))
    return sorted(found)


def _cpu() -> float:
    """CPU seconds of this process and the children it has waited for."""
    # Not `os.times`, whose 10 ms ticks read a whole test as nothing.
    own = resource.getrusage(resource.RUSAGE_SELF)
    waited = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + waited.ru_utime + waited.ru_stime


def _wall(test: dict) -> float:
    """A test's baseline wall seconds, its phases summed."""
    return sum(test["wall"].values())


def _baseline(session, state, rundir):
    """Child 0: the selection run once under the tracer. Returns what it
    wrote: the reports, and per test its lines, times and outcome."""
    events = os.path.join(rundir, "baseline.jsonl")
    out = os.path.join(rundir, "baseline.out")
    mapfile = os.path.join(rundir, "map.json")
    config = session.config

    def child():
        _recapture(config, out)
        state.tracer.resume()
        recorder = _Recorder(events, full=True)
        prescan = _Prescan(state.tracer)
        config.pluginmanager.register(recorder, "invective-recorder")
        config.pluginmanager.register(prescan, "invective-prescan")
        _run_items(session.items, lambda: False)
        _teardown(session, recorder)
        state.tracer.pause()
        tests = {}
        for item in session.items:
            lines: dict[str, set[int]] = {}
            keys = [(item.nodeid, phase) for phase in ("setup", "call", "teardown")]
            keys += [("fixture", fid) for fid in prescan.fixtures.get(item.nodeid, [])]
            for key in keys:
                for path, run in state.tracer.lines.get(key, {}).items():
                    lines.setdefault(path, set()).update(run)
            unrun = dict.fromkeys(("setup", "call", "teardown"), 0.0)
            tests[item.nodeid] = {"lines": {p: sorted(l) for p, l in lines.items()},
                                  "wall": unrun | prescan.wall.get(item.nodeid, {}),
                                  "cpu": unrun | prescan.cpu.get(item.nodeid, {})}
        with open(mapfile, "w", encoding="utf-8") as fh:
            json.dump({"tests": tests, "import": state.tracer.lines_of(tracer.IMPORT),
                       "files": sorted(state.tracer.files)}, fh)
        recorder.write({"event": "done"})

    result = _one(children.Job("baseline", child, out, None), 1)
    lines = _events(events)
    if not lines or lines[-1].get("event") != "done":
        raise Refusal("the baseline run %s without finishing; its output ends:\n%s"
                      % (children.describe_status(result.status), children.tail(out)))
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


def _one(job: children.Job, workers: int) -> children.Done:
    got: list[children.Done] = []
    children.run_all([job], workers, got.append)
    return got[0]


def _events(path: str) -> list[dict]:
    """The JSON lines of *path*, a torn last line dropped."""
    found = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    found.append(json.loads(line))
                except ValueError:
                    break
    except OSError:
        pass
    return found


def _replay(session, reports: list[dict]) -> None:
    """The baseline's reports through the parent's hooks, so that the
    terminal shows the selection's run as pytest would have."""
    from _pytest.reports import TestReport
    config = session.config
    by_test: dict[str, list[dict]] = {}
    for line in reports:
        by_test.setdefault(line["nodeid"], []).append(line["json"])
    for item in session.items:
        if item.nodeid not in by_test:
            continue
        config.hook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        for data in by_test[item.nodeid]:
            config.hook.pytest_runtest_logreport(report=TestReport._from_json(data))
        config.hook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)


def _activate(state: State, path: str | None, ordinal: int, identity: str) -> None:
    """*ordinal* live in the target *path*, and every other target left as
    it is in the parent, running its original code; with no path, *ordinal*
    live in every target (the canary). An ordinal numbers a mutant within
    its own module's table."""
    for each in sorted(state.targets) if path is None else [path]:
        switch.activate(each, state.finder.modules.get(each, []), ordinal, identity)


def _child_run(session, state, events, out, path, ordinal, identity, items):
    """The body of a child that runs *items* with *ordinal* of *path* live,
    stopping at the first failure, and writes its verdict before the final
    teardown."""
    config = session.config
    _recapture(config, out)
    _activate(state, path, ordinal, identity)
    recorder = _Recorder(events, full=False)
    config.pluginmanager.register(recorder, "invective-recorder")
    _run_items(items, lambda: recorder.failed is not None)
    if recorder.failed is None:
        recorder.write({"verdict": "survived"})
    else:
        recorder.write({"verdict": "killed", "killer": recorder.failed.nodeid,
                        "when": recorder.failed.when, "canary": recorder.failed.canary})
    _teardown(session, recorder)


def _canary(session, state, rundir, items) -> str:
    """The canary child; returns the test whose failure raised `switch.Canary`,
    or refuses when the first failure was anything else."""
    events = os.path.join(rundir, "canary.jsonl")
    out = os.path.join(rundir, "canary.out")
    _one(children.Job("canary", lambda: _child_run(
        session, state, events, out, None, switch.CANARY, "canary", items), out, None), 1)
    verdict = _verdict_line(events)
    if verdict is None or verdict.get("verdict") != "killed":
        raise Refusal("the forced-fail canary survived: a mutant that raises in every "
                      "instrumented function failed no test of the %d that ran the "
                      "targets, so the targets' code is not what the tests run"
                      % len(items))
    if not verdict.get("canary"):
        raise Refusal("the forced-fail canary was live, but the failure was not the "
                      "canary's: %s (%s) failed for a reason of its own, which proves "
                      "nothing about whether the tests run the targets' code; it may "
                      "depend on a test that does not run the targets"
                      % (verdict["killer"], verdict["when"]))
    return verdict["killer"]


def _verdict_line(events: str) -> dict | None:
    return next((line for line in _events(events) if "verdict" in line), None)


def _covering(mutant: Mutant, path: str, tests: dict, order: list[str]) -> list[str]:
    """The tests that ran a line of *mutant*'s span, cheapest first; *path*
    is the target's real path, which keys the map."""
    first, last = mutant.span
    found = []
    for nodeid in order:
        lines = tests[nodeid]["lines"].get(path)
        if lines and any(first <= line <= last for line in lines):
            found.append(nodeid)
    return found


def _mutants(session, state, tables, rundir, tests, order, items, workers) -> list[Verdict]:
    # By the function's own code object, not by its lines: a one-line
    # function's `def` line is its body's line too, and the module runs it.
    ran_at_import = state.tracer.functions_of(tracer.IMPORT)
    verdicts: dict[str, Verdict] = {}
    jobs = []
    for path in sorted(state.targets):
        info = tables[path]
        at_import = ran_at_import.get(path, set())
        for mutant in info.mutants:
            covering = _covering(mutant, path, tests, order)
            if mutant.qualname in at_import:
                verdicts[mutant.id] = Verdict(
                    mutant, "unmeasured", None, None, len(covering),
                    "the function ran at import, before any switch; the slow path "
                    "is not built yet")
                continue
            if not covering:
                verdicts[mutant.id] = Verdict(mutant, "unreached", None, None, 0,
                                              "no test ran the function in the baseline")
                continue
            budget = max(FLOOR, MULTIPLIER * sum(_wall(tests[n]) for n in covering))
            events = os.path.join(rundir, "%d.jsonl" % len(jobs))
            out = os.path.join(rundir, "%d.out" % len(jobs))
            jobs.append(children.Job(
                (mutant, covering, events, out),
                _runner(session, state, events, out, path, mutant,
                        [items[n] for n in covering]),
                out, budget))

    def done(result: children.Done) -> None:
        mutant, covering, events, out = result.key
        line = _verdict_line(events)
        if line is not None:
            verdicts[mutant.id] = Verdict(mutant, line["verdict"], line.get("killer"),
                                          line.get("when"), len(covering), "")
        elif result.timed_out:
            verdicts[mutant.id] = Verdict(mutant, "unmeasured", None, None, len(covering),
                                          "over its wall budget of %.1f s, unconfirmed"
                                          % jobs_budget[mutant.id])
        else:
            verdicts[mutant.id] = Verdict(
                mutant, "unmeasured", None, None, len(covering),
                "the child %s without a verdict; its output ends:\n%s"
                % (children.describe_status(result.status), children.tail(out, 10)))

    jobs_budget = {job.key[0].id: job.budget for job in jobs}
    children.run_all(jobs, workers, done)
    ordered = [m for path in sorted(state.targets) for m in tables[path].mutants]
    return [verdicts[m.id] for m in ordered]


def _runner(session, state, events, out, path, mutant, items):
    return lambda: _child_run(session, state, events, out, path, mutant.ordinal,
                              mutant.id, items)
