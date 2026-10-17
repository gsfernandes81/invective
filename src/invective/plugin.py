"""The pytest plugin: `pytest --mutate <path>` on the session the user started.

The plugin installs the finder and starts the tracer at
`pytest_load_initial_conftests`, before any conftest can import a target. It takes
over `pytest_runtestloop`: the engine runs every test in forked children and the
parent runs none. The engine passes the baseline's reports through the parent's
hooks, so the terminal shows the selection's run as pytest would have shown it, and
the mutation report follows it.
"""

from __future__ import annotations

import os
from typing import NoReturn

import pytest

from invective import engine, report
from invective.errors import Refusal

_STATE = pytest.StashKey[engine.State]()
_REFUSAL = pytest.StashKey[str]()
_REPORT = pytest.StashKey[engine.Report]()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add `--mutate`, `--mutate-workers` and `--mutate-json`."""
    group = parser.getgroup("invective", "mutation testing")
    group.addoption(
        "--mutate",
        action="append",
        default=[],
        metavar="PATH",
        help="a file or directory to mutate; may be given more than once. "
        "The rest of the command line is the selection of tests.",
    )
    group.addoption(
        "--mutate-workers",
        default=None,
        metavar="N",
        help="mutants run at once; 'auto' or the default is one per CPU",
    )
    group.addoption(
        "--mutate-json",
        default=None,
        metavar="FILE",
        help="write every mutant with its verdict as JSON",
    )


def pytest_load_initial_conftests(early_config: pytest.Config) -> None:
    """With `--mutate`, set up the engine's state before any conftest is imported.

    A refusal at this point is kept, and `pytest_configure` reports it.
    """
    paths = early_config.known_args_namespace.mutate
    if not paths:
        return
    try:
        if not hasattr(os, "fork"):
            raise Refusal("this platform cannot fork; the slow path is not built yet")
        targets = engine.targets_of(
            paths,
            str(early_config.invocation_params.dir),
            early_config.getini("python_files"),
        )
        early_config.stash[_STATE] = engine.State(targets)
    except Refusal as exc:
        early_config.stash[_REFUSAL] = str(exc)


def pytest_configure(config: pytest.Config) -> None:
    """With `--mutate`, stop the session if the run is refused before collection.

    It is refused for a refusal kept from `pytest_load_initial_conftests`, and when
    pytest-xdist workers are asked for.
    """
    if not config.getoption("mutate"):
        return
    if config.stash.get(_REFUSAL, None):
        _refuse(config, config.stash[_REFUSAL])
    if config.getoption("numprocesses", None):
        _refuse(
            config,
            "--mutate runs the collected tests itself, one mutant at a time; "
            "turn pytest-xdist's workers off with -n 0",
        )


def _refuse(config: pytest.Config, why: str) -> NoReturn:
    """Print the refusal *why* and stop the session with exit status 2."""
    _say(config, f"invective refused the run: {why}")
    pytest.exit("invective refused the run", returncode=2)


def _say(config: pytest.Config, line: str) -> None:
    """Write *line* to the terminal, through pytest's reporter when it has one."""
    tr = config.pluginmanager.get_plugin("terminalreporter")
    (tr.write_line if tr is not None else print)(line)


def _workers(config: pytest.Config) -> int:
    """Return how many mutant children run at once, from `--mutate-workers`.

    `auto`, or no value, is one per CPU this process may run on. Raises
    `pytest.UsageError` for anything but a count from 1.
    """
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
def pytest_runtestloop(session: pytest.Session) -> bool | None:
    """With `--mutate`, run the engine in place of pytest's own test loop.

    The JSON report is written here, to the file `--mutate-json` names. Without
    `--mutate`, or with `--collect-only`, pytest's own loop runs.
    """
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


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    """Write the mutation report as the `invective` section of the summary."""
    result = config.stash.get(_REPORT, None)
    if result is None:
        return
    terminalreporter.section("invective")
    for line in report.lines(result, str(config.invocation_params.dir)):
        terminalreporter.write_line(line)
