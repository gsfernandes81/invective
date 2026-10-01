"""invective's pytest plugin.

`pytest --mutate MODULE [pytest's own selection]` breaks MODULE one edit at a
time and runs the tests pytest collected against each edit, every one in a
fresh pytest process of its own, then reports the edits no test noticed.
Without `--mutate` the plugin does nothing.

It also reports from inside each of those runs: when `INVECTIVE_VERDICT`
names a file, the first test to fail is written there as the session ends.
That is how the engine learns which test killed a mutant.

**Nothing from `invective` is imported unless `--mutate` is given.** pytest
loads this plugin before a repository's own `pythonpath` setting takes
effect, so a package imported here comes from wherever it is installed. In a
run of invective against its own code, that is the checkout and not the
worktree that holds the mutant, and every mutant would survive.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

#: The environment variable that names the file a run's verdict goes to.
VERDICT = "INVECTIVE_VERDICT"

_REPORTS = pytest.StashKey[list]()


def pytest_addoption(parser):
    group = parser.getgroup("invective", "mutation testing (invective)")
    group.addoption(
        "--mutate", action="append", default=[], metavar="MODULE",
        help="break MODULE one edit at a time and run the collected tests "
             "against each edit. May be given more than once.")
    group.addoption(
        "--mutate-only", metavar="OPS",
        help="comma-separated kinds of edit to make, as `invective run "
             "--only` takes them")
    group.addoption(
        "--mutate-limit", type=int, metavar="N",
        help="make at most N edits per module, evenly spaced through it")
    group.addoption(
        "--mutate-json", metavar="FILE",
        help="write the reports to FILE as well, as a JSON list")


def pytest_configure(config):
    # Popped, not read: a run of the suite can start pytest again, and that
    # run must not write over this one's verdict.
    path = os.environ.pop(VERDICT, None)
    if path:
        config.pluginmanager.register(_Verdict(config, path), "invective-verdict")

    if not config.getoption("mutate"):
        return
    if config.getoption("numprocesses", None):
        # Under pytest-xdist the controlling process collects nothing, so the
        # selection a mutant would be run against is empty.
        raise pytest.UsageError(
            "--mutate runs the collected tests itself, one mutant at a time; "
            "turn pytest-xdist's workers off with -n 0")
    from invective.mutate import OPERATORS
    unknown = [k for k in _only(config) or [] if k not in OPERATORS]
    if unknown:
        raise pytest.UsageError("--mutate-only: unknown kind(s) of edit: %s"
                                % ", ".join(unknown))


def _only(config):
    given = config.getoption("mutate_only")
    return [k.strip().upper() for k in given.split(",")] if given else None


class _Verdict:
    """The first test to fail in this run, written where the engine asked."""

    def __init__(self, config, path):
        self.config, self.path, self.killer = config, path, ""

    def pytest_collectreport(self, report):
        self._note(report)

    def pytest_runtest_logreport(self, report):
        self._note(report)

    def _note(self, report):
        if report.failed and not self.killer:
            # As pytest's own summary prints it: relative to where the run
            # started, which is the top of the worktree.
            self.killer = self.config.cwd_relative_nodeid(report.nodeid)

    def pytest_sessionfinish(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"killer": self.killer}, fh)


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    config = session.config
    targets = config.getoption("mutate")
    # pytest's own loop stops a run whose collection failed, and only lists
    # the tests of a --collect-only one.
    if not targets or session.testsfailed or config.option.collectonly:
        return None
    from invective import mutate

    tr = config.pluginmanager.get_plugin("terminalreporter")
    say = tr.write_line if tr is not None else print
    reports = []
    try:
        if not session.items:
            # An empty selection is no selection: run as it stands, pytest
            # would run every test it could find instead.
            raise mutate.Refusal("pytest collected no tests, so there is "
                                 "nothing to notice a mutant")
        root = mutate.repo_root()
        selection = [_node(item, root) for item in session.items]
        with tempfile.TemporaryDirectory(prefix="invective-") as box:
            # One node id a line, read by pytest's own `@file`: a selection
            # of thousands is too long for a Windows command line.
            listed = os.path.join(box, "selection.txt")
            with open(listed, "w", encoding="utf-8") as fh:
                fh.write("".join(node + "\n" for node in selection))
            for target in targets:
                report = mutate.mutate(root, target, ["@" + listed],
                                       _only(config),
                                       config.getoption("mutate_limit"),
                                       say=say)
                report["tests"] = selection
                reports.append(report)
    except mutate.Refusal as exc:
        if tr is not None:
            tr.write_line("refused: %s" % exc, red=True, bold=True)
        else:
            print("refused: %s" % exc)
        pytest.exit("invective refused the run", returncode=2)

    config.stash[_REPORTS] = reports
    out = config.getoption("mutate_json")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(reports, fh, indent=1)
    return True


def _node(item, root):
    """The item's node id, its file given from *root*, where every run starts."""
    path = os.path.relpath(str(item.path), root).replace(os.sep, "/")
    _file, sep, rest = item.nodeid.partition("::")
    return path + sep + rest


def pytest_terminal_summary(terminalreporter, config):
    from_run = config.stash.get(_REPORTS, [])
    if not from_run:
        return
    from invective.mutate import summary

    for report in from_run:
        terminalreporter.section("invective: %s" % report["target"])
        for line in summary(report):
            terminalreporter.write_line(line)
