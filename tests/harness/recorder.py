"""A pytest plugin the harness loads, with `-p`, into each pytest it starts.

It writes every test's start, every test report, every failed collection and the
session's exit status to the file `RESULTS` names, one JSON line each, opening and
closing the file for every line, and its hooks run ahead of the project's own. A run
that a test or a conftest hook ends with `os._exit`, or whose descriptors a test
closes, still leaves the lines written before. With `SELECT` set
to a file of node ids, one a line, the run keeps those tests only, in that order.

The harness copies this file under `MODULE`'s name into a directory of its own on
`PYTHONPATH`, so that no other module of the suite is importable in the run.
"""

from __future__ import annotations

import json
import os

import pytest

#: The module name the harness loads this plugin under.
MODULE = "invective_harness_recorder"

#: The variable naming the file the plugin writes, as an absolute path.
RESULTS = "INVECTIVE_HARNESS_RESULTS"

#: The variable naming the file of node ids the run keeps, when set.
SELECT = "INVECTIVE_HARNESS_SELECT"

#: The results file, read once at configuration: a test may clear the environment.
_OUT: list[str] = []


def _write(line: dict[str, object]) -> None:
    """Append *line* to the results file as one JSON line."""
    if _OUT:
        with open(_OUT[0], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")


def pytest_configure() -> None:
    """Keep the results file's path from the environment."""
    path = os.environ.get(RESULTS)
    if path:
        _OUT.append(path)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """With `SELECT` set, keep the tests its file names, in its order."""
    path = os.environ.get(SELECT)
    if not path:
        return
    with open(path, encoding="utf-8") as fh:
        wanted = [line.rstrip("\n") for line in fh if line.strip()]
    by_id = {item.nodeid: item for item in items}
    kept = [by_id[nodeid] for nodeid in wanted if nodeid in by_id]
    config.hook.pytest_deselected(items=[item for item in items if item not in kept])
    items[:] = kept


@pytest.hookimpl(tryfirst=True)
def pytest_collectreport(report: pytest.CollectReport) -> None:
    """Write a collection that failed."""
    if report.failed:
        _write({"event": "collect", "nodeid": report.nodeid})


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logstart(nodeid: str) -> None:
    """Write the start of a test, before its setup."""
    _write({"event": "start", "nodeid": nodeid})


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Write a test's report for one phase."""
    _write(
        {
            "event": "report",
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
        }
    )


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(exitstatus: int) -> None:
    """Write the session's exit status."""
    _write({"event": "finish", "exitstatus": int(exitstatus)})
