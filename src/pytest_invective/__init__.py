"""invective's pytest plugin.

`pytest --mutate MODULE [pytest's own selection]` breaks MODULE one edit at a
time and runs the tests pytest collected against each edit, every one in a
fresh pytest process of its own, then reports the edits no test noticed.
Without `--mutate` the plugin does nothing.

It also works from inside each of those runs. When `INVECTIVE_SELECTION`
names a file of node ids, one a line, the run collects their files and keeps
those tests and no others. When `INVECTIVE_VERDICT` names a file, the first
test to fail, and any selected test not found, are written there as the
session ends. That is how the engine learns which test killed a mutant.

**Nothing from `invective` is imported unless `--mutate` is given.** pytest
before 8.4 loads plugins before a repository's own `pythonpath` setting takes
effect, and every pytest loads them before any `conftest.py`, so a package
imported here comes from wherever it is installed. In a run of invective
against its own code, that is the checkout and not the copy that holds
the mutant, and every mutant would survive.
"""

from __future__ import annotations

import json
import os

import pytest

#: The environment variable that names the file a run's verdict goes to.
VERDICT = "INVECTIVE_VERDICT"
#: The environment variable that names the file of node ids a run keeps.
SELECTION = "INVECTIVE_SELECTION"

_REPORTS = pytest.StashKey[list]()
_FAILURES = pytest.StashKey[list]()
_WANTED = pytest.StashKey[list]()
_MISSING = pytest.StashKey[list]()


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
    group.addoption(
        "--mutate-ref", metavar="REF",
        help="make the mutants from this git commit, branch or tag instead of "
             "the files as they stand")


def pytest_load_initial_conftests(early_config, parser, args):
    # Popped, not read, as the verdict's variable is below.
    path = os.environ.pop(SELECTION, None)
    if path is None:
        return
    with open(path, encoding="utf-8") as fh:
        wanted = fh.read().splitlines()
    early_config.stash[_WANTED] = wanted
    # Their files, to collect; `pytest_collection_modifyitems` keeps the tests.
    args.extend(dict.fromkeys(node.partition("::")[0] for node in wanted))


def pytest_collection_modifyitems(config, items):
    wanted = config.stash.get(_WANTED, None)
    if wanted is None:
        return
    root = str(config.invocation_params.dir)
    want = set(wanted)
    keep, drop, found = [], [], set()
    for item in items:
        key = _key(item, root)
        found.add(key)
        (keep if key in want else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
    config.stash[_MISSING] = [node for node in wanted if node not in found]


def pytest_configure(config):
    # Popped, not read: a run of the suite can start pytest again, and that
    # run must not write over this one's verdict.
    path = os.environ.pop(VERDICT, None)
    if path:
        config.pluginmanager.register(_Verdict(config, path), "invective-verdict")

    if not config.getoption("mutate"):
        return
    if path:
        # Every mutant's run would start a campaign of its own, and each of
        # theirs another, without end.
        raise pytest.UsageError(
            "--mutate reached a mutant's own run; is it in PYTEST_ADDOPTS or "
            "the repository's addopts? Give it on the command line only.")
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
            # started, which is the top of the copy. Rewritten that way,
            # the file's path has Windows separators on Windows, and a node
            # id spells its path with `/` everywhere.
            path, sep, rest = self.config.cwd_relative_nodeid(
                report.nodeid).partition("::")
            self.killer = path.replace(os.sep, "/") + sep + rest

    def pytest_sessionfinish(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"killer": self.killer,
                       "missing": self.config.stash.get(_MISSING, [])}, fh)


# First: a plugin registered after this one would otherwise be asked first.
@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    config = session.config
    targets = config.getoption("mutate")
    # pytest's own loop only lists the tests of a --collect-only run.
    if not targets or config.option.collectonly:
        return None
    from invective import mutate

    from invective import config as settings
    from invective import tree

    tr = config.pluginmanager.get_plugin("terminalreporter")
    say = tr.write_line if tr is not None else print
    root = str(config.invocation_params.dir)
    reports, failures = [], []
    try:
        if session.testsfailed:
            # Even with --continue-on-collection-errors: the tests that did
            # collect are not the selection that was asked for.
            raise mutate.Refusal("%d error(s) during collection, so the "
                                 "selection is not the one asked for"
                                 % session.testsfailed)
        if not session.items:
            # An empty selection is no selection: run as it stands, pytest
            # would run every test it could find instead.
            raise mutate.Refusal("pytest collected no tests, so there is "
                                 "nothing to notice a mutant")
        rules = settings.load(root)
        selection = [_node(item, root) for item in session.items]
        ref = config.getoption("mutate_ref")
        for target in targets:
            report = mutate.mutate(
                root, target, [], _only(config),
                config.getoption("mutate_limit"), say=say,
                tree=(tree.git_ref(root, ref) if ref
                      else tree.working_tree(root, rules.exclude)),
                selection=selection, options=_forwarded(config))
            reports.append(report)
            failures.extend("%s: %s" % (report["target"], failure)
                            for failure in mutate.gate(report, rules))
    except mutate.Refusal as exc:
        say("refused: %s" % exc)
        pytest.exit("invective refused the run", returncode=2)

    config.stash[_REPORTS] = reports
    config.stash[_FAILURES] = failures
    if tr is None:
        # `pytest_terminal_summary` belongs to the terminal plugin, and
        # without it the score would never be shown.
        for line in _closing(reports, failures):
            print(line)
    out = config.getoption("mutate_json")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(reports, fh, indent=1)
    # A run that broke the project's own rules fails as a failing test would.
    session.testsfailed += len(failures)
    return True


_ENGINE_S = frozenset({"terminal", "xdist", "xdist.plugin", __name__})


#: The options of the run that change how its tests behave, which each
#: mutant's run is given too. Anything else stays behind: `--pdb` would stop
#: a run for good, `--lf` would drop tests, and `-q` would leave a refusal
#: nothing to quote.
def _forwarded(config):
    forwarded = []
    for plugin in config.option.plugins or ():
        # The plugins whose options every run is given (`-rf`, `--no-header`,
        # `-n 0`) or that report its verdict stay as the engine sets them.
        if plugin.removeprefix("no:") not in _ENGINE_S:
            forwarded += ["-p", plugin]
    for override in config.option.override_ini or ():
        forwarded += ["-o", override]
    for warning in config.getoption("pythonwarnings", None) or ():
        forwarded += ["-W", warning]
    forwarded.append("--import-mode=%s" % config.option.importmode)
    for flag, dest in (("--runxfail", "runxfail"),
                       ("--strict-markers", "strict_markers"),
                       ("--doctest-modules", "doctestmodules")):
        if config.getoption(dest, False):
            forwarded.append(flag)
    return tuple(forwarded)


def _key(item, root):
    """The item's node id, its file given from *root*, where every run starts."""
    try:
        path = os.path.relpath(str(item.path), root)
    except ValueError:
        # Windows: the test is on another drive than the project.
        path = os.pardir
    _file, sep, rest = item.nodeid.partition("::")
    return path.replace(os.sep, "/") + sep + rest


def _node(item, root):
    """`_key`, for a test that has to be in the copy every run starts in."""
    from invective.mutate import Refusal

    node = _key(item, root)
    if node == os.pardir or node.startswith(os.pardir + "/"):
        raise Refusal("%s is outside the project at %s, so no copy of it "
                      "has it" % (item.nodeid, root))
    return node


def _closing(reports, failures):
    from invective.mutate import summary

    for report in reports:
        yield from summary(report)
    for failure in failures:
        yield "fails:     %s" % failure


def pytest_terminal_summary(terminalreporter, config):
    from_run = config.stash.get(_REPORTS, [])
    if not from_run:
        return
    from invective.mutate import summary

    for report in from_run:
        terminalreporter.section("invective: %s" % report["target"])
        for line in summary(report):
            terminalreporter.write_line(line)
    for failure in config.stash.get(_FAILURES, []):
        terminalreporter.write_line("fails:     %s" % failure)
