"""A crash reporter's stand-in: it ends the process once a failed test has torn down."""

import os

import pytest

_failed: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Exit the process with status 3 at the teardown report of a test that failed."""
    if report.failed:
        _failed.append(report.nodeid)
    if report.when == "teardown" and report.nodeid in _failed:
        os._exit(3)
