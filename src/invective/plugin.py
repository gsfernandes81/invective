"""The pytest plugin: `pytest --mutate <path>` on the session the user started.

The finder and the tracer go in at `pytest_load_initial_conftests`, before
any conftest can import a target. `pytest_runtestloop` is taken over: the
engine runs every test in forked children and the parent runs none. The
baseline's reports are replayed through the parent's hooks, so the terminal
shows the selection's run as pytest would have, and the mutation report
follows it.
"""

from __future__ import annotations

import os
import sys

import pytest

from invective import engine, report
from invective.errors import Refusal

_STATE = pytest.StashKey()
_REFUSAL = pytest.StashKey()
_REPORT = pytest.StashKey()


def pytest_addoption(parser):
    group = parser.getgroup("invective", "mutation testing")
    group.addoption("--mutate", action="append", default=[], metavar="PATH",
                    help="a file or directory to mutate; may be given more than once. "
                         "The rest of the command line is the selection of tests.")
    group.addoption("--mutate-workers", default=None, metavar="N",
                    help="mutants run at once; 'auto' or the default is one per CPU")
    group.addoption("--mutate-json", default=None, metavar="FILE",
                    help="write every mutant with its verdict as JSON")


def pytest_load_initial_conftests(early_config, parser, args):
    paths = early_config.known_args_namespace.mutate
    if not paths:
        return
    try:
        if not hasattr(os, "fork"):
            raise Refusal("this platform cannot fork; the slow path is not built yet")
        targets = engine.targets_of(paths, str(early_config.invocation_params.dir),
                                    early_config.getini("python_files"))
        early_config.stash[_STATE] = engine.State(targets)
    except Refusal as exc:
        early_config.stash[_REFUSAL] = str(exc)


def pytest_configure(config):
    if not config.getoption("mutate"):
        return
    if config.stash.get(_REFUSAL, None):
        _refuse(config, config.stash[_REFUSAL])
    if config.getoption("numprocesses", None):
        _refuse(config, "--mutate runs the collected tests itself, one mutant at a time; "
                        "turn pytest-xdist's workers off with -n 0")


def _refuse(config, why: str) -> None:
    _say(config, "invective refused the run: %s" % why)
    pytest.exit("invective refused the run", returncode=2)


def _say(config, line: str) -> None:
    tr = config.pluginmanager.get_plugin("terminalreporter")
    (tr.write_line if tr is not None else print)(line)


def _workers(config) -> int:
    given = config.getoption("mutate_workers")
    if given in (None, "auto"):
        try:
            return len(os.sched_getaffinity(0))
        except AttributeError:
            return os.cpu_count() or 1
    try:
        workers = int(given)
    except ValueError:
        workers = 0
    if workers < 1:
        raise pytest.UsageError("--mutate-workers takes a count from 1, or 'auto'")
    return workers


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    config = session.config
    state = config.stash.get(_STATE, None)
    if state is None or config.option.collectonly:
        return None
    try:
        result = engine.run(session, state, _workers(config))
    except Refusal as exc:
        _refuse(config, str(exc))
    config.stash[_REPORT] = result
    out = config.getoption("mutate_json")
    here = str(config.invocation_params.dir)
    if out:
        report.write_json(result, os.path.join(here, out), here)
    return True


def pytest_terminal_summary(terminalreporter, config):
    result = config.stash.get(_REPORT, None)
    if result is None:
        return
    terminalreporter.section("invective")
    for line in report.lines(result, str(config.invocation_params.dir)):
        terminalreporter.write_line(line)
