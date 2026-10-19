"""The pytest plugin: `pytest --mutate <path>` on the session the user started.

The plugin installs the finder and starts the tracer at
`pytest_load_initial_conftests`, before any conftest can import a target. It takes
over `pytest_runtestloop`: the engine runs every test in forked children and the
parent runs none. The engine passes the baseline's reports through the parent's
hooks, so the terminal shows the selection's run as pytest would have shown it, and
the mutation report follows it.

Without `--mutate`, a run with `environment.MUTANT` set runs the one mutant it names:
the plugin installs the finder for that mutant's file and makes the mutant live before
any conftest is imported, and pytest runs its tests as it always does.

The plugin imports the engine and the report inside its hooks, after the finder is
installed, so that a run on invective's own tree can mutate them. The files it
imports before then are `HARNESS`: a target among them is left out of the run, and
the report names it.
"""

from __future__ import annotations

import fnmatch
import os
from typing import TYPE_CHECKING, NoReturn

import pytest

from invective import environment, finder, switch
from invective.errors import Refusal

if TYPE_CHECKING:
    from invective import engine

#: The files of the running invective, relative to its package, that are imported
#: before the finder is installed: this module and what it imports. Each is in
#: `sys.modules` before the finder exists, so none can be served instrumented.
HARNESS = frozenset(
    {
        "__init__.py",
        "environment.py",
        "errors.py",
        "finder.py",
        "instrument.py",
        "operators/__init__.py",
        "operators/fn_value.py",
        "plugin.py",
        "switch.py",
    }
)

#: The running invective's package directory, as a real path.
_PACKAGE = os.path.dirname(finder.real(__file__))

_STATE: pytest.StashKey[engine.State] = pytest.StashKey()
_REFUSAL = pytest.StashKey[str]()
_REPORT: pytest.StashKey[engine.Report] = pytest.StashKey()


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

    Without it, make the mutant `environment.MUTANT` names live, if it names one. A
    refusal at this point is kept, and `pytest_configure` reports it.
    """
    paths = early_config.known_args_namespace.mutate
    here = str(early_config.invocation_params.dir)
    if not paths:
        try:
            _activate_named(here)
        except Refusal as exc:
            early_config.stash[_REFUSAL] = str(exc)
        return
    try:
        if not hasattr(os, "fork"):
            raise Refusal("this platform cannot fork; the slow path is not built yet")
        found = targets_of(paths, here, early_config.getini("python_files"))
        skipped = {path for path in found if _is_harness(path)}
        if skipped == found:
            raise pytest.UsageError(
                "--mutate: every file named is part of invective's harness, which is "
                "imported before the finder exists: "
                + ", ".join(os.path.relpath(path, here) for path in sorted(skipped))
            )
        targets = found - skipped
        installed = finder.install(targets)
        # Not imported at module level: a target among the engine's modules is
        # served instrumented only when it is first imported after `install`.
        from invective import engine

        early_config.stash[_STATE] = engine.State(targets, installed, skipped)
    except Refusal as exc:
        early_config.stash[_REFUSAL] = str(exc)


def _activate_named(here: str) -> None:
    """Make live the mutant `environment.MUTANT` names, as `<path>:<ordinal>`.

    A relative path is taken from *here*. The value `canary`, or no value, makes
    nothing live. Raises `pytest.UsageError` for a value that names no file or no
    mutant of its file's table, and `Refusal` when the file is already imported.
    """
    value = os.environ.get(environment.MUTANT, "")
    if value in ("", "canary"):
        return
    given, _, number = value.rpartition(":")
    path = finder.real(os.path.join(here, given))
    if not number.isdigit() or not os.path.isfile(path):
        raise pytest.UsageError(
            f"{environment.MUTANT}={value!r}: expected <path>:<ordinal> naming a file"
        )
    ordinal = int(number)
    if ordinal not in {mutant.ordinal for mutant in finder.table(path).mutants}:
        raise pytest.UsageError(
            f"{environment.MUTANT}={value!r}: {given} has no mutant {ordinal}"
        )
    finder.install({path})
    switch.activate(path, [], ordinal, value)


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


def _is_harness(path: str) -> bool:
    """Return whether the real *path* is one of the `HARNESS` files of this invective."""
    return os.path.relpath(path, _PACKAGE).replace(os.sep, "/") in HARNESS


def pytest_configure(config: pytest.Config) -> None:
    """Stop the session if the run is refused before collection.

    It is refused for a refusal kept from `pytest_load_initial_conftests`, and, with
    `--mutate`, when pytest-xdist workers are asked for.
    """
    if config.stash.get(_REFUSAL, None):
        _refuse(config, config.stash[_REFUSAL])
    if not config.getoption("mutate"):
        return
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
    from invective import engine, report

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
    from invective import report

    terminalreporter.section("invective")
    for line in report.lines(result, str(config.invocation_params.dir)):
        terminalreporter.write_line(line)
