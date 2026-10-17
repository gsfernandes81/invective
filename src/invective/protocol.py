"""Running collected tests inside a forked child, and the events file it writes.

Every child runs its tests through pytest's own `pytest_runtest_protocol`, on the
session the parent collected, with its output in a file of its own. A `Recorder`
registered in the child writes each test report as one JSON line of the child's
events file, which the parent reads after the child has ended. Nothing else a child
does reaches the parent.

`recapture`, `run_items`, `teardown` and `Recorder` run in a mutant's child, between
the fork and the verdict line, and are marked `@switch.harness`, so that a self-run
gives them no guard and no mutant: the canary raising in `Recorder` would stop every
child answering.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Generator
from typing import IO, NamedTuple

import pytest

from invective import switch


@switch.harness
def recapture(config: pytest.Config, out: str) -> None:
    """Restart pytest's global capture in the child, over the file *out*.

    After the call, the child's stdout and stderr are *out*, and pytest's capture
    restores *out* between tests, with temporary capture files of the child's own.
    """
    # The parent's capture restores the terminal between tests and captures into
    # one temporary file that every child would share. Stopping it puts the terminal
    # back on descriptors 1 and 2, so the child's file goes over them before a
    # fresh capture saves what it restores.
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


@switch.harness
def run_items(items: list[pytest.Item], stop: Callable[[], bool]) -> None:
    """Run *items* in order through `pytest_runtest_protocol`.

    Each item is told its successor, so a fixture of a wider scope stays set up
    across the items that share it. The run ends after the first item for which
    *stop* returns true, and whatever is set up then stays set up for `teardown`.
    """
    for index, item in enumerate(items):
        nextitem = items[index + 1] if index + 1 < len(items) else None
        item.ihook.pytest_runtest_protocol(item=item, nextitem=nextitem)
        if stop():
            break


@switch.harness
def teardown(session: pytest.Session, recorder: Recorder) -> None:
    """Tear down everything still set up in the child.

    An exception a finalizer raises, of any class, is written as a
    `teardown-raised` event naming the class, and goes no further. A finalizer can
    raise `switch.Canary`, a `pytest.fail` or `pytest.skip` outcome, or
    `SystemExit`, none of them an `Exception`, so a caller writes its verdict before
    it calls this.
    """
    try:
        session._setupstate.teardown_exact(None)
    except BaseException as exc:
        recorder.write({"event": "teardown-raised", "type": type(exc).__qualname__})


class Failure(NamedTuple):
    """The first test report of a child's run that failed."""

    nodeid: str
    when: str | None  #: the phase: setup, call or teardown
    canary: bool  #: the failure's exception is `switch.Canary`


@switch.harness
class Recorder:
    """A pytest plugin, registered in a child, that writes the child's events file.

    Every test report becomes one JSON line of the file, flushed as it is written,
    so the lines a child wrote before it died are there for the parent to read.
    """

    def __init__(self, path: str, *, full: bool) -> None:
        """Write to the events file *path*, which the `with` block opens.

        With *full*, each report line also carries the whole report, which the
        parent replays through its own hooks.
        """
        self.path = path
        self.full = full
        #: The first report that failed, or None while none has.
        self.failed: Failure | None = None
        self._canaries: set[tuple[str, str | None]] = set()
        self._fh: IO[str] | None = None

    def __enter__(self) -> Recorder:
        """Open the events file for writing, emptying it, and return the recorder."""
        self._fh = open(self.path, "w", encoding="utf-8")
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the events file."""
        if self._fh is not None:
            self._fh.close()

    def write(self, line: dict[str, object]) -> None:
        """Write *line* as one JSON line of the events file, and flush it."""
        assert self._fh is not None, "write called outside the recorder's `with` block"
        self._fh.write(json.dumps(line) + "\n")
        self._fh.flush()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self, call: pytest.CallInfo[None]
    ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
        """Note a failed phase whose exception is `switch.Canary`."""
        report = yield
        if (
            report.failed
            and call.excinfo is not None
            and call.excinfo.type is switch.Canary
        ):
            self._canaries.add((report.nodeid, report.when))
        return report

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Write *report* as a `report` event, and keep it if it is the first failure."""
        line: dict[str, object] = {
            "event": "report",
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration": report.duration,
        }
        if self.full:
            line["json"] = report._to_json()
        self.write(line)

        if report.failed and self.failed is None:
            self.failed = Failure(
                report.nodeid,
                report.when,
                (report.nodeid, report.when) in self._canaries,
            )


def read_events(path: str) -> list[dict]:
    """Return the JSON lines of the events file *path*, oldest first.

    A torn last line, from a child that died while writing it, is left out, and a
    missing file reads as no lines.
    """
    found = []
    with contextlib.suppress(OSError), open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                found.append(json.loads(line))
            except ValueError:
                break
    return found
