"""The run, in the parent: child 0, the canary, then one child per mutant.

The parent is the pytest session the user started, after collection. No test,
fixture or mutant runs in the parent, and it imports a target only when a conftest or
a test module imports it during collection: each target's mutant table is built from
its source. Every test runs in a child forked from the parent, in this order:

- child 0 runs the whole selection once under the tracer (`baseline`), which is the
  green-baseline check and the map of the lines each test runs;
- the canary child (`mutants.canary`) checks that the selection runs the
  instrumented code;
- each mutant's covering tests run in a child of their own (`mutants.run`).

Before the first mutant, the engine refuses the run (`errors.Refusal`) when the
selection is empty or red, when it runs no line of the targets, when the canary is
not killed by `switch.Canary`, or when a thread is alive in the parent at a fork.
"""

from __future__ import annotations

import fnmatch
import gc
import os
import shutil
import tempfile
import threading
import time
from typing import NamedTuple

import pytest

from invective import baseline, children, environment, finder, mutants, tracer
from invective.errors import Refusal
from invective.instrument import Instrumented

#: A red baseline's refusal names this many of its failing tests.
_SHOWN = 5


class State:
    """What the plugin sets up before any conftest is imported.

    That is the targets, the finder that serves them instrumented, and the tracer,
    which runs from then on.
    """

    def __init__(self, targets: set[str]) -> None:
        """Start the tracer and install the finder for *targets*, as real paths.

        Raises `Refusal` when a target is already imported.
        """
        self.targets = targets
        self.tracer = tracer.Tracer(targets)
        self.tracer.start()
        self.finder = finder.install(targets)


class Context(NamedTuple):
    """What every child of a run is forked with."""

    session: pytest.Session
    state: State
    rundir: str  #: the run's directory, which holds every child's files


class Report(NamedTuple):
    """What a run found, for the console report and the JSON file."""

    targets: list[str]
    verdicts: list[mutants.Verdict]
    baseline: dict  #: the baseline's map, with its report events
    canary: str  #: the test the forced-fail canary failed
    seconds: float


def targets_of(paths: list[str], here: str, test_patterns: list[str]) -> set[str]:
    """Return the real paths of the files *paths* name, relative to *here*.

    A directory stands for every `.py` file under it that is neither a conftest nor
    a test module by *test_patterns*, outside hidden directories, `__pycache__` and
    any directory that holds a `pyvenv.cfg`. Raises `pytest.UsageError` for a path
    that does not exist, and when the paths hold nothing to mutate.
    """
    found: set[str] = set()
    for given in paths:
        path = os.path.join(here, given)
        if os.path.isfile(path):
            found.add(finder.real(path))
        elif os.path.isdir(path):
            found.update(_modules_under(path, test_patterns))
        else:
            raise pytest.UsageError(f"--mutate: no such file or directory: {given}")

    if not found:
        raise pytest.UsageError(f"--mutate: nothing to mutate under {', '.join(paths)}")
    return found


def _modules_under(directory: str, test_patterns: list[str]) -> list[str]:
    """Return the real paths of the modules `targets_of` takes under *directory*."""
    found = []
    for top, dirs, files in os.walk(directory):
        dirs[:] = sorted(
            name
            for name in dirs
            if not name.startswith(".")
            and name != "__pycache__"
            and not os.path.exists(os.path.join(top, name, "pyvenv.cfg"))
        )
        found += [
            finder.real(os.path.join(top, name))
            for name in sorted(files)
            if name.endswith(".py")
            and name != "conftest.py"
            and not any(fnmatch.fnmatch(name, pattern) for pattern in test_patterns)
        ]
    return found


def run(session: pytest.Session, state: State, workers: int) -> Report:
    """Run every mutant of the targets against the collected *session*.

    Every test runs in a child, *workers* children at a time. Raises `Refusal`, with
    the cause, when a check that comes before the first mutant fails.
    """
    started = time.monotonic()
    if session.testsfailed:
        raise Refusal(
            f"{session.testsfailed} error(s) during collection, so the selection is "
            "not the one asked for"
        )
    if not session.items:
        raise Refusal("pytest collected no tests, so there is nothing to notice a mutant")
    tables = _tables(state)
    _refuse_threads()

    # A forked child shares the parent's memory pages copy-on-write. The first
    # garbage collection in a child writes reference counts and GC headers into
    # every object it examines, which copies the pages they sit on, so each child
    # would copy much of the parent for nothing. `gc.freeze` moves every object the
    # parent holds into a permanent generation the collector does not examine;
    # `gc.collect` runs first so that garbage is not frozen with them.
    gc.collect()
    gc.freeze()
    # The tracer turned on line events for every target code object that ran, and
    # those events would fire in every child, so the tracer is paused before the
    # first fork and no mutant child pays for them. Child 0 resumes it.
    state.tracer.pause()

    rundir = tempfile.mkdtemp(prefix="invective-")
    context = Context(session, state, rundir)
    try:
        data, tests = baseline.run(context)
        _replay(session, data["reports"])
        # The replay runs the project's report hooks in the parent, and one of them
        # may start a thread before the canary's fork.
        _refuse_threads()
        _refuse_red(tests, rundir)

        covering_any = [
            nodeid
            for nodeid, test in tests.items()
            if any(test["lines"].get(path) for path in state.targets)
        ]
        if not covering_any:
            raise Refusal("no test in the selection runs a line of the targets")
        items = {item.nodeid: item for item in session.items}
        canary = mutants.canary(context, [items[nodeid] for nodeid in covering_any])
        verdicts = mutants.run(context, tables, tests, workers)
    finally:
        if not os.environ.get(environment.KEEP):
            shutil.rmtree(rundir, ignore_errors=True)

    return Report(
        sorted(state.targets), verdicts, data, canary, time.monotonic() - started
    )


def _refuse_threads() -> None:
    """Refuse the run, naming them, if threads other than the main one are alive.

    A thread does not survive a fork, and a lock it held stays held in the child,
    where the next call that takes it waits forever.
    """
    alive = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
    if alive:
        raise Refusal(
            "threads are alive in the parent, so a forked child could deadlock: "
            + ", ".join(alive)
        )


def _refuse_red(tests: dict, rundir: str) -> None:
    """Refuse the run if a test failed in the baseline.

    The refusal names the first `_SHOWN` failing tests and ends with the baseline's
    output, read from *rundir* before the engine removes it.
    """
    failed = [nodeid for nodeid, test in tests.items() if test["outcome"] == "failed"]
    if not failed:
        return

    shown = ", ".join(failed[:_SHOWN])
    if len(failed) > _SHOWN:
        shown += f" and {len(failed) - _SHOWN} more"
    output = children.tail(os.path.join(rundir, baseline.OUT))
    ending = f"; the run's output ends:\n{output}" if output.strip() else ""
    raise Refusal(f"the selection fails before any mutant: {shown}{ending}")


def _tables(state: State) -> dict[str, Instrumented]:
    """Return every target's mutant table, keyed by real path, without importing it.

    Refuses the run, naming the target, when a target cannot be read as Python.
    """
    tables = {}
    for path in sorted(state.targets):
        try:
            tables[path] = finder.table(path)
        except (SyntaxError, ValueError, OSError) as exc:
            raise Refusal(f"{path} cannot be read as Python: {exc}") from exc
    return tables


def _replay(session: pytest.Session, reports: list[dict]) -> None:
    """Pass the baseline's reports through the parent's hooks, test by test.

    The terminal then shows the selection's run as pytest would have shown it, with
    pytest's own count of passed and failed tests.
    """
    config = session.config
    by_test: dict[str, list[dict]] = {}
    for line in reports:
        by_test.setdefault(line["nodeid"], []).append(line["json"])

    for item in session.items:
        if item.nodeid not in by_test:
            continue
        config.hook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        for data in by_test[item.nodeid]:
            config.hook.pytest_runtest_logreport(
                report=pytest.TestReport._from_json(data)
            )
        config.hook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
