"""The run: a pristine parent, child 0 for the baseline and the map, the
canary, then one forked child per mutant, and the verdicts read back.

The parent is the pytest session the user started, after collection. It
never runs a test: every test runs in a child forked from it. Child 0 runs
the whole selection once, which is the green-baseline check and the
pre-scan in one run. The canary child makes every instrumented function
raise and must see a failure, or the run is refused: the proof that the
selection runs instrumented code. Then each mutant's covering tests run in
a child of their own, cheapest first, stopping at the first failure, and
the child's verdict is a line in a file the parent reads after `waitpid`.
"""

from __future__ import annotations

import fnmatch
import gc
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from typing import NamedTuple

import pytest

from invective import children, finder, switch, tracer
from invective.errors import Refusal
from invective.instrument import Mutant

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
    _import_targets(state)
    alive = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
    if alive:
        raise Refusal("threads are alive in the parent, so a forked child could "
                      "deadlock: %s" % ", ".join(alive))
    gc.collect()
    gc.freeze()
    state.tracer.pause()
    rundir = tempfile.mkdtemp(prefix="invective-")
    try:
        baseline, tests = _baseline(session, state, rundir)
        _replay(session, baseline["reports"])
        failed = [nodeid for nodeid, test in tests.items() if test["outcome"] == "failed"]
        if failed:
            raise Refusal("the selection fails before any mutant: %s (its run is in %s)"
                          % (", ".join(failed[:5]), os.path.join(rundir, "baseline.out")))
        items = {item.nodeid: item for item in session.items}
        order = sorted(tests, key=lambda nodeid: tests[nodeid]["wall"])
        covering_any = [nodeid for nodeid in tests
                        if any(tests[nodeid]["lines"].get(path) for path in state.targets)]
        if not covering_any:
            raise Refusal("no test in the selection runs a line of the targets")
        canary = _canary(session, state, rundir, [items[n] for n in covering_any])
        verdicts = _mutants(session, state, rundir, tests, order, items, workers)
    finally:
        if not os.environ.get(KEEP):
            shutil.rmtree(rundir, ignore_errors=True)
    return Report(sorted(state.targets), verdicts, baseline, canary,
                  time.monotonic() - started)


def _import_targets(state: State) -> None:
    """Every target imported in the parent, so that its mutants are known
    before the first fork. The project's own imports did most of this during
    collection; the rest are imported by the name their path gives."""
    for path in sorted(state.targets):
        if path in state.finder.instrumented:
            continue
        name = finder.module_name(path)
        try:
            importlib.import_module(name)
        except ImportError as exc:
            raise Refusal("%s cannot be imported as %s: %s" % (path, name, exc)) from exc
        if path not in state.finder.instrumented:
            raise Refusal("%s imports as %s, but that module is %s" % (
                path, name, getattr(sys.modules.get(name), "__file__", "elsewhere")))


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


def _run_items(session, items, stop) -> None:
    """*items* through `pytest_runtest_protocol` in order, each told its
    successor so that fixtures of a wider scope live across them, stopping
    when *stop* says to; then everything still set up is torn down."""
    for index, item in enumerate(items):
        nextitem = items[index + 1] if index + 1 < len(items) else None
        item.ihook.pytest_runtest_protocol(item=item, nextitem=nextitem)
        if stop():
            break
    try:
        session._setupstate.teardown_exact(None)
    except Exception:
        pass


class _Recorder:
    """Registered in a child: each report as one line, and the first failure."""

    def __init__(self, path: str, full: bool):
        self.fh = open(path, "w", encoding="utf-8")
        self.full = full
        self.failed: tuple[str, str] | None = None

    def write(self, line: dict) -> None:
        self.fh.write(json.dumps(line) + "\n")
        self.fh.flush()

    def pytest_runtest_logreport(self, report):
        line = {"event": "report", "nodeid": report.nodeid, "when": report.when,
                "outcome": report.outcome, "duration": report.duration}
        if self.full:
            line["json"] = report._to_json()
        self.write(line)
        if report.failed and self.failed is None:
            self.failed = (report.nodeid, report.when)


class _Prescan:
    """Registered in child 0: the tracer's context follows the test, its
    phase and the fixture being set up, and each test's fixtures are kept."""

    def __init__(self, tracer_):
        self.tracer = tracer_
        self.fixtures: dict[str, list[int]] = {}
        self.cpu: dict[str, float] = {}

    def _phase(self, item, phase):
        self.tracer.context = (item.nodeid, phase)
        before = _cpu()
        try:
            return (yield)
        finally:
            self.cpu[item.nodeid] = self.cpu.get(item.nodeid, 0.0) + _cpu() - before
            self.tracer.context = None

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item):
        try:
            yield from self._phase(item, "setup")
        finally:
            request = getattr(item, "_request", None)
            defs = getattr(request, "_fixture_defs", None) or {}
            self.fixtures[item.nodeid] = [id(d) for d in defs.values()]

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        yield from self._phase(item, "call")

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item):
        yield from self._phase(item, "teardown")

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        before = self.tracer.context
        self.tracer.context = ("fixture", id(fixturedef))
        try:
            return (yield)
        finally:
            self.tracer.context = before


def _cpu() -> float:
    times = os.times()
    return times.user + times.system + times.children_user + times.children_system


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
        _run_items(session, session.items, lambda: False)
        state.tracer.pause()
        tests = {}
        for item in session.items:
            lines: dict[str, set[int]] = {}
            keys = [(item.nodeid, phase) for phase in ("setup", "call", "teardown")]
            keys += [("fixture", fid) for fid in prescan.fixtures.get(item.nodeid, [])]
            for key in keys:
                for path, run in state.tracer.lines.get(key, {}).items():
                    lines.setdefault(path, set()).update(run)
            tests[item.nodeid] = {"lines": {p: sorted(l) for p, l in lines.items()},
                                  "cpu": prescan.cpu.get(item.nodeid, 0.0)}
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
        tests[nodeid]["wall"] = 0.0
        tests[nodeid]["outcome"] = "passed"
    for line in reports:
        test = tests.get(line["nodeid"])
        if test is None:
            continue
        test["wall"] += line["duration"]
        if line["outcome"] == "failed":
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


def _activate(state: State, path: str | None, ordinal: int | None, identity: str) -> None:
    """*ordinal* live in the modules of the target *path*, None in every
    other; with no path, in every instrumented module (the canary). An
    ordinal numbers a mutant within its own module's table."""
    # The live module goes last: `switch.activate` with None clears the
    # environment's identity, and the live call must be the one that sets it.
    paths = sorted(state.finder.modules, key=lambda each: each == path)
    for each in paths:
        live = ordinal if path is None or each == path else None
        switch.activate(state.finder.modules[each], state.finder.names.get(each, []),
                        live, identity)


def _child_run(session, state, events, out, path, ordinal, identity, items):
    """The body of a child that runs *items* with *ordinal* of *path* live,
    stopping at the first failure, and writes its verdict last."""
    config = session.config
    _recapture(config, out)
    _activate(state, path, ordinal, identity)
    recorder = _Recorder(events, full=False)
    config.pluginmanager.register(recorder, "invective-recorder")
    _run_items(session, items, lambda: recorder.failed is not None)
    if recorder.failed is None:
        recorder.write({"verdict": "survived"})
    else:
        recorder.write({"verdict": "killed", "killer": recorder.failed[0],
                        "when": recorder.failed[1]})


def _canary(session, state, rundir, items) -> str:
    """The canary child; returns the test it failed, or refuses."""
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
    return verdict["killer"]


def _verdict_line(events: str) -> dict | None:
    lines = _events(events)
    return lines[-1] if lines and "verdict" in lines[-1] else None


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


def _mutants(session, state, rundir, tests, order, items, workers) -> list[Verdict]:
    imported = state.finder.instrumented
    verdicts: dict[str, Verdict] = {}
    jobs = []
    for path in sorted(state.targets):
        info = imported[path]
        at_import = set()
        for line in state.tracer.lines_of(tracer.IMPORT).get(path, []):
            at_import.add(line)
        for mutant in info.mutants:
            covering = _covering(mutant, path, tests, order)
            first, last = mutant.span
            if any(first <= line <= last for line in at_import):
                verdicts[mutant.id] = Verdict(
                    mutant, "unmeasured", None, None, len(covering),
                    "the function ran at import, before any switch; the slow path "
                    "is not built yet")
                continue
            if not covering:
                verdicts[mutant.id] = Verdict(mutant, "unreached", None, None, 0,
                                              "no test ran the function in the baseline")
                continue
            budget = max(FLOOR, MULTIPLIER * sum(tests[n]["wall"] for n in covering))
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
    ordered = [m for path in sorted(state.targets) for m in imported[path].mutants]
    return [verdicts[m.id] for m in ordered]


def _runner(session, state, events, out, path, mutant, items):
    return lambda: _child_run(session, state, events, out, path, mutant.ordinal,
                              mutant.id, items)
