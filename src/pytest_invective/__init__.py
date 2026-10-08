"""invective's pytest plugin.

`pytest --mutate MODULE [pytest's own selection]` breaks MODULE one edit at a
time and runs the tests pytest collected against each edit, every one in a
fresh pytest process of its own, then reports the edits no test noticed.
Without `--mutate` the plugin does nothing.

It also works from inside each of those runs. When `INVECTIVE_SELECTION`
names a file of node ids, one a line, the run collects their files and keeps
those tests and no others; the last `INVECTIVE_TYPED` arguments of its
command line then only start pytest's search for settings. When
`INVECTIVE_VERDICT` names a file, the first test to fail, and any selected
test not found, are written there as the session ends. That is how the
engine learns which test killed a mutant. When `INVECTIVE_TARGET` names the
mutated module, the verdict also says whether the tests loaded that module
from somewhere other than the copy they ran in.

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
import sys
import sysconfig

import pytest

#: The environment variable that names the file a run's verdict goes to.
VERDICT = "INVECTIVE_VERDICT"
#: The environment variable that names the file of node ids a run keeps.
SELECTION = "INVECTIVE_SELECTION"
#: The environment variable that names the mutated module, as a path from
#: the top of the copy with `/` separators.
TARGET = "INVECTIVE_TARGET"
#: The environment variable that holds how many arguments at the end of a
#: selection's run are paths pytest's search for settings starts from, and
#: not tests to collect.
TYPED = "INVECTIVE_TYPED"

#: The modules the interpreter and pytest had loaded as this plugin was
#: imported. The engine asks for the plugin with `-p`, which pytest imports
#: before its entry-point plugins, `PYTEST_PLUGINS` and every `conftest.py`;
#: a module in here was loaded by the harness and not by the tests, so it is
#: not where the tests got the target from. This matters because a pytest
#: process holds about a hundred bare-name modules of the standard library
#: (`platform`, `types`, `inspect`, `string`, ...), and a loose target of
#: the project named like one of them would otherwise be refused naming the
#: library's file. A plugin given in `PYTEST_ADDOPTS` or the project's own
#: `addopts` is imported before this one, so whatever it imported is in
#: here too: `_loaded_elsewhere` says how far that exemption goes.
_LOADED_BEFORE = frozenset(sys.modules)

#: The directories of the interpreter's own library, each ending in a
#: separator. No project's file is in them, and `_LOADED_BEFORE` holds only
#: what pytest loaded, not what the tests load: a test that imports
#: `logging.handlers` or `queue` puts the library's module under the name a
#: project's file can be given.
_LIBRARY = tuple(sorted({
    os.path.join(os.path.normcase(os.path.realpath(
        sysconfig.get_paths()[key])), "")
    for key in ("stdlib", "platstdlib")}))

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
    group.addoption(
        "--mutate-workers", metavar="N",
        help="run N mutants at once, each in a copy of its own, as `invective "
             "run --workers` does")


def pytest_load_initial_conftests(early_config, parser, args):
    # Popped, not read, as the verdict's variable is below.
    path = os.environ.pop(SELECTION, None)
    if path is None:
        return
    with open(path, encoding="utf-8") as fh:
        wanted = fh.read().splitlines()
    early_config.stash[_WANTED] = wanted
    # pytest has found its settings from the paths the run was given. They
    # are dropped before it loads a `conftest.py` for them or collects them:
    # the run stands for one that collected the selection alone.
    typed = int(os.environ.pop(TYPED, "0"))
    if typed:
        del args[-typed:]
        given = early_config.known_args_namespace
        given.file_or_dir = given.file_or_dir[:-typed]
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
    target = os.environ.pop(TARGET, None)
    if path:
        config.pluginmanager.register(_Verdict(config, path, target),
                                      "invective-verdict")

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
    """The first test to fail in this run, written where the engine asked,
    with the file the tests loaded the target from when it is not the copy's.
    """

    def __init__(self, config, path, target=None):
        self.config, self.path, self.killer = config, path, ""
        self.target = target

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
        # At the session's end and from inside the run, not from `sys.path`
        # up front: what matters is which file the tests actually loaded,
        # after every conftest, `.pth` line, `pythonpath` setting and import
        # hook had its say, and only the process that ran them knows.
        elsewhere = ""
        if self.target:
            elsewhere = _loaded_elsewhere(str(self.config.invocation_params.dir),
                                          self.target)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"killer": self.killer,
                       "missing": self.config.stash.get(_MISSING, []),
                       "elsewhere": elsewhere}, fh)


def _loaded_elsewhere(copy, target):
    """The file the tests loaded *target* from when it is outside *copy*, the
    directory every run starts in; `""` when they loaded the copy's, or
    nothing under its name at all.

    *target* is a path from the top of the copy with `/` separators. The
    name the tests import it by is read from the copy as `invective.sweep`
    does (this module must not import `invective`, see its docstring): the
    directories above it that hold an `__init__.py` are its package, so
    `src/pkg/sub/mod.py` is `pkg.sub.mod` with the anchored path
    `pkg/sub/mod.py`, and `pkg/__init__.py` is `pkg`. A loose file, with no
    `__init__.py` beside it, is its bare stem and has no anchored path. The
    walk up stops at the first directory with no `__init__.py`, so a target
    in a namespace package, at the top or inside a regular package, is a
    loose file here, and one in a regular package below a namespace package
    is named from that package down. Neither is the name the tests import
    it by. When another module holds that name, the target is cleared if
    the copy's own file is loaded under any name, and is otherwise judged
    by the name as it stands; when nothing holds it, the target is not
    checked.

    **The interpreter's own library is never named.** No project's file is
    in it, and `_LOADED_BEFORE` holds only what pytest loaded: a test that
    imports `logging.handlers` puts the library's module under the name that
    `src/acme/logging/handlers.py` is given here when `acme` is a namespace
    package, so a hit in the library's directories is the harness's and not
    the project's. A site directory inside the library's directory, as an
    interpreter outside a virtual environment has, is not the library: a
    project installed there is a file to name.

    **The name decides whenever it can.** `sys.modules` is keyed by import
    name, so the module under the target's name is one lookup: under the
    copy, nothing is wrong, and that holds when it is a different file of
    the copy than the target (the tests import `src/pkg` while the target is
    a copy of the package elsewhere in the tree), which is a decision: the
    run is then unchanged. Any other file is the one named, unless the
    copy's own file is loaded under some other name, which is the mutant
    the tests saw. An editable install's `.pth` line, or a `sys.path`
    entry, puts the project's own `src` ahead of the copy's exactly this
    way.

    **A name in `_LOADED_BEFORE` was loaded by the harness, not the tests,
    and is let be** -- unless the target has an anchored path and the module
    lies at it outside the copy, `.../pkg/sub/mod.py`, which is the target's
    own layout somewhere else: a plugin in the project's `addopts` that
    imports the target is imported before this module, and what it loaded is
    what the tests got. A loose target keeps the whole exemption, since its
    bare stem is what the standard library's modules are named by.

    **Under a suffix of the name, only when nothing is under the name.** A
    `sys.path` entry inside the package (`src/pkg` on the path, so the tests
    say `import gate`) registers the target under a shorter name. Each proper
    dotted suffix (`sub.mod`, then `mod`) is looked up, and a module there
    is the target only when it lies outside the copy at the anchored path.
    A lookup and not a scan of every module's file for that tail: pytest's
    own packages give every process `_pytest/config/__init__.py`,
    `_pytest/main.py` and two hundred more tails a project's module can be
    named like, and a scan named them for a target the tests never imported.
    """
    copy = os.path.normcase(os.path.realpath(copy))
    inside = os.path.join(copy, "")
    parts = target.split("/")
    home = os.path.join(copy, *parts[:-1])
    package = []
    # Never past the copy's top: the run starts there, so that is the
    # directory on `sys.path`, and nothing above it is.
    while home != copy and os.path.isfile(os.path.join(home, "__init__.py")):
        package.insert(0, os.path.basename(home))
        home = os.path.dirname(home)
    stem, _ext = os.path.splitext(parts[-1])
    names = package if stem == "__init__" else package + [stem]
    name = ".".join(names)
    anchored = (os.path.normcase(os.path.join(*package, parts[-1]))
                if package else "")

    def found(name):
        """The resolved file of the module loaded under *name*, or None."""
        module = sys.modules.get(name)
        # This plugin, by identity: pytest loads it from wherever invective
        # is installed, which in a run of invective on its own code is the
        # checkout, and its mutants are seen by the tests that start pytest
        # afresh. By `__name__` and not a list, because it is the one module
        # invective itself asks pytest to load before any conftest can
        # redirect it; a project's own plugin that is the target is checked
        # like any other module, and refused when the tests cannot see it.
        if module is None or module is sys.modules.get(__name__):
            return None
        # A `.pyc`, a frozen or built-in module and an extension are not a
        # file the mutant could be in. Joined to the copy because a relative
        # `sys.path` entry gives a relative `__file__`, relative to the
        # directory the run started in.
        try:
            file = getattr(module, "__file__", None)
        except Exception:
            # A lazily loaded module can fail on any attribute.
            return None
        if not isinstance(file, str) or not file.endswith(".py"):
            return None
        # Resolved but spelt as the filesystem spells it, since this is the
        # path the refusal names; only the comparisons below go through
        # `normcase`, which on Windows lowercases the whole path.
        file = os.path.realpath(os.path.join(copy, file))
        return None if _in_the_library(os.path.normcase(file)) else file

    def decide():
        file = found(name)
        # A loose target is a module file, so a package's `__init__.py`
        # under its bare name is another package's, which the tests
        # imported and the loose file is not.
        if (file is not None and not package
                and os.path.normcase(os.path.basename(file))
                != os.path.normcase(parts[-1])):
            file = None
        if file is not None:
            key = os.path.normcase(file)
            if (name not in _LOADED_BEFORE
                    or (anchored and key.endswith(os.sep + anchored))):
                return "" if key.startswith(inside) else file
        if anchored:
            for n in range(1, len(names)):
                suffix = ".".join(names[n:])
                if suffix in _LOADED_BEFORE:
                    continue
                file = found(suffix)
                if file is None:
                    continue
                key = os.path.normcase(file)
                if not key.startswith(inside) and key.endswith(os.sep + anchored):
                    return file
        return ""

    elsewhere = decide()
    # Only on the way to a refusal, so an ordinary run pays nothing for the
    # scan, and a scan that can only clear makes no refusal of its own.
    if elsewhere and _is_loaded(copy, os.path.normcase(os.path.realpath(
            os.path.join(copy, *parts)))):
        return ""
    return elsewhere


def _in_the_library(file):
    """Whether *file*, resolved, is one of the interpreter's own library's,
    and not in a site directory inside the library's directory."""
    for library in _LIBRARY:
        if file.startswith(library):
            rest = file[len(library):].split(os.sep)
            return "site-packages" not in rest and "dist-packages" not in rest
    return False


def _is_loaded(copy, mine):
    """Whether a module in `sys.modules` was loaded from *mine*, a resolved
    file of the copy at *copy*, under whatever name."""
    for module in list(sys.modules.values()):
        try:
            file = getattr(module, "__file__", None)
        except Exception:
            # A lazily loaded module can fail on any attribute.
            continue
        if isinstance(file, str) and os.path.normcase(os.path.realpath(
                os.path.join(copy, file))) == mine:
            return True
    return False


# First: a plugin registered after this one would otherwise be asked first.
# invective: accept[equivalent: True -> False] nothing registered later takes the loop today
@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    config = session.config
    targets = config.getoption("mutate")
    # pytest's own loop only lists the tests of a --collect-only run.
    if not targets or config.option.collectonly:
        return None
    from invective import mutate

    from invective import config as settings

    tr = config.pluginmanager.get_plugin("terminalreporter")
    say = tr.write_line if tr is not None else print
    # The project's top, not the directory pytest was started in: from a
    # subdirectory that one would be copied alone, and the tests above it
    # and the project's settings left behind. Node ids are built from it
    # below, not from pytest's rootdir, so they are what every run, started
    # at the top of the copy, can find whatever pytest decided.
    root = settings.project_root(str(config.invocation_params.dir))
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
        workers = config.getoption("mutate_workers")
        if workers is None:
            workers = rules.workers
        else:
            settings.workers(workers, "--mutate-workers")
        selection = [_node(item, root) for item in session.items]
        # Every run starts at the top of the copy and collects the
        # selection's files. Its pytest's search for settings starts where
        # this one's did, from the same paths given from the top, and a
        # file named with `-c` is given from the top too, so that one
        # inside the project is the tree's own.
        here = str(config.invocation_params.dir)
        typed = mutate.rewrite_tests(_searched_from(config), here, root)
        options = _forwarded(config)
        if config.option.inifilename:
            options += ("-c", *mutate.rewrite_tests(
                [config.option.inifilename], here, root))
        for target in targets:
            report = mutate.mutate(
                root, target, typed, _only(config),
                config.getoption("mutate_limit"), say=say,
                ref=config.getoption("mutate_ref"), exclude=rules.exclude,
                selection=selection, options=options, workers=workers)
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
            # invective: accept[equivalent: 1 -> 2] how wide the JSON is indented
            json.dump(reports, fh, indent=1)
    # A run that broke the project's own rules fails as a failing test would.
    session.testsfailed += len(failures)
    # invective: accept[equivalent: True -> False] pluggy stops at any result that is not None
    return True


def _searched_from(config):
    """The paths pytest's search for settings started from: those among the
    run's arguments that are there, as typed, or with none the directory it
    was started in."""
    here = str(config.invocation_params.dir)
    return [arg for arg in config.option.file_or_dir
            if os.path.exists(os.path.join(here, arg.partition("::")[0]))
            ] or [here]


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


def _given_from(path, root):
    """*path*, an absolute path, given from *root*; None when it is not
    inside it, by a `..` or, on Windows, by another drive."""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel


def _key(item, root):
    """The item's node id, its file given from *root*, where every run starts;
    a file outside *root* is given as `..`."""
    # `invective.config.relative_to_root`'s rule, repeated because importing
    # invective here would load the checkout's package into a mutant's run:
    # pytest keeps a path given as typed, and an absolute one typed from a
    # working directory entered through a link to the project leads through
    # the link, so it is given from the root once both are resolved.
    path = _given_from(str(item.path), root)
    if path is None:
        path = _given_from(os.path.realpath(item.path), os.path.realpath(root))
    if path is None:
        path = os.pardir
    _file, sep, rest = item.nodeid.partition("::")
    return path.replace(os.sep, "/") + sep + rest


def _node(item, root):
    """`_key`, for a test that has to be in the copy every run starts in."""
    from invective.mutate import Refusal

    node = _key(item, root)
    if node.partition("::")[0] == os.pardir:
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
