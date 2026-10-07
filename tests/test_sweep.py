"""The sweep: which modules it mutates, and which tests it runs against each."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from invective import mutate, sweep

from conftest import (FILES, SLOW_TEST, SRC, WORKSPACE, stop_group,
                      wait_for, write_tree)

SOURCES = ["pkg", "loose"]
TESTS_DIR = "pkg/tests"


def _p(rel):
    return os.path.join(*rel.split("/"))


def test_the_suite_s_own_files_are_never_mutation_targets(tree):
    """`conftest.py` and `helpers.py` do not start `test_`, and are not modules.

    A harness file is imported by nearly every test, so mutating one re-runs
    most of the suite per mutant. The directory beside the suite whose name
    merely starts the same way is still source.
    """
    found = set(sweep.modules(tree, SOURCES, TESTS_DIR))

    assert found == {_p(m) for m in (
        "pkg/__init__.py", "pkg/gate.py", "pkg/idle.py",
        "pkg/sub/__init__.py", "pkg/sub/deep.py",
        "pkg/tests_extra/aid.py", "loose/tool.py")}


@pytest.mark.parametrize("module, name", [
    ("pkg/gate.py", "pkg.gate"),
    ("pkg/sub/deep.py", "pkg.sub.deep"),
    ("pkg/sub/__init__.py", "pkg.sub"),
    ("pkg/__init__.py", "pkg"),
    ("loose/tool.py", None),
])
def test_a_module_is_named_as_its_tests_import_it(tree, module, name):
    assert sweep.import_name(tree, _p(module), SOURCES) == name


def test_a_package_under_src_is_named_from_src(tmp_path):
    """The import root is the first directory above the package that is not one."""
    write_tree(str(tmp_path), {"src/pkg/__init__.py": "",
                               "src/pkg/a/b.py": "x = 1\n"})

    # `a` holds no `__init__.py` and is still part of the dotted name: the
    # package is the directory `--src` named, not each directory below it.
    assert sweep.import_name(str(tmp_path), _p("src/pkg/a/b.py"),
                             ["src/pkg"]) == "pkg.a.b"


def test_a_package_below_a_source_directory_that_is_not_one_is_named_from_its_own_top(
        tmp_path):
    """`--src src` over `src/pkg/` still finds the tests that import `pkg.gate`."""
    root = str(tmp_path)
    write_tree(root, {"src/pkg/__init__.py": "",
                      "src/pkg/gate.py": "x = 1\n",
                      "tests/sub/test_x.py": "from pkg import gate\n",
                      "scripts/tool.py": "x = 1\n"})

    assert sweep.import_name(root, _p("src/pkg/gate.py"), ["src"]) == "pkg.gate"
    assert sweep.covering(root, _p("src/pkg/gate.py"), ["src"], "tests") == [
        _p("tests/sub/test_x.py")]
    # A directory of loose files stays loose.
    assert sweep.import_name(root, _p("scripts/tool.py"), ["scripts"]) is None


@pytest.mark.parametrize("line", [
    "import pkg.gate",
    "from pkg.gate import admit",
    "from pkg import gate",
    "from pkg import idle, gate",
    "x = pkg.gate.admit(30, False)",
])
def test_a_test_file_covers_the_module_it_imports(tree, line):
    write_tree(tree, {"pkg/tests/test_other.py": line + "\n"})

    assert _p("pkg/tests/test_other.py") in sweep.covering(
        tree, _p("pkg/gate.py"), SOURCES, TESTS_DIR)


@pytest.mark.parametrize("line", [
    "# the gate is checked elsewhere",
    "name = 'gate'",
    "from pkg import idle",
    "import pkg.gateway",
])
def test_a_name_in_passing_is_not_an_import(tree, line):
    """Matching the bare word would select most of a suite for a common name."""
    write_tree(tree, {"pkg/tests/test_other.py": line + "\n"})

    assert _p("pkg/tests/test_other.py") not in sweep.covering(
        tree, _p("pkg/gate.py"), SOURCES, TESTS_DIR)


def test_a_loose_module_is_covered_by_its_bare_import(tree):
    write_tree(tree, {"pkg/tests/test_other.py": "from tool import tool\n"})

    assert sweep.covering(tree, _p("loose/tool.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_other.py"), _p("pkg/tests/test_tool.py")]


def test_the_file_named_after_a_module_covers_it_without_importing_it(tree):
    write_tree(tree, {"pkg/tests/test_idle.py": "def test_nothing():\n    pass\n"})

    assert sweep.covering(tree, _p("pkg/idle.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_idle.py")]


def test_a_test_file_in_a_subdirectory_covers_the_module_it_imports(tree):
    """A suite grouped into directories (`tests/browser/`) is still the suite.

    The top-level files come first and each subdirectory follows in name
    order, so the map is the same list on every run.
    """
    write_tree(tree, {
        "pkg/tests/sub/test_idle_deep.py": "from pkg import idle\n",
        "pkg/tests/sub/more/test_idle_deeper.py": "from pkg import idle\n",
        "pkg/tests/aaa/test_idle_first.py": "from pkg import idle\n",
        "pkg/tests/test_idle_top.py": "from pkg import idle\n",
        # Not tests: a harness file and a helper in the subdirectory.
        "pkg/tests/sub/conftest.py": "from pkg import idle\n",
        "pkg/tests/sub/helpers.py": "from pkg import idle\n",
    })

    assert sweep.covering(tree, _p("pkg/idle.py"), SOURCES, TESTS_DIR) == [
        _p(f) for f in ("pkg/tests/test_idle_top.py",
                        "pkg/tests/aaa/test_idle_first.py",
                        "pkg/tests/sub/test_idle_deep.py",
                        "pkg/tests/sub/more/test_idle_deeper.py")]


def test_the_file_named_after_a_module_covers_it_from_the_mirror_directory(tree):
    write_tree(tree, {"pkg/tests/sub/test_deep.py": "def test_nothing():\n    pass\n"})

    assert sweep.covering(tree, _p("pkg/sub/deep.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_deep_things.py"), _p("pkg/tests/sub/test_deep.py")]


def test_a_file_named_after_a_module_elsewhere_is_another_module_s(tmp_path):
    """`models.py` and `test_models.py` recur in every app of a mirrored
    layout. The file named after a module covers it at the top of the tests
    directory and where the module's own directory is mirrored, and anywhere
    else only by importing it."""
    root = str(tmp_path / "proj")
    write_tree(root, {
        "src/app/__init__.py": "",
        "src/app/api/__init__.py": "",
        "src/app/api/models.py": "X = 1\n",
        "src/app/db/__init__.py": "",
        "src/app/db/models.py": "X = 2\n",
        "tests/api/test_models.py": "from app.api import models\n",
        "tests/zz/test_models.py": "def test_nothing():\n    pass\n",
    })

    assert sweep.covering(root, "src/app/db/models.py", ["src/app"], "tests") == []
    assert sweep.covering(root, "src/app/api/models.py", ["src/app"], "tests") == [
        os.path.join("tests", "api", "test_models.py")]
    write_tree(root, {"tests/api/test_models.py": "def test_nothing():\n    pass\n"})
    assert sweep.covering(root, "src/app/api/models.py", ["src/app"], "tests") == [
        os.path.join("tests", "api", "test_models.py")]


def test_a_loose_module_is_covered_by_its_bare_stem_only_where_its_name_is(
        tmp_path):
    """A loose module's tests import it by its bare stem, and a bare stem in a
    subdirectory is as likely that directory's own helper or an attribute of
    something else. A module in a package is imported by its dotted path,
    which counts anywhere."""
    root = str(tmp_path / "proj")
    write_tree(root, {
        "scripts/config.py": "X = 1\n",
        "tools/helpers.py": "X = 2\n",
        "pkg/__init__.py": "",
        "pkg/app.py": "X = 3\n",
        "tests/web/helpers.py": "X = 4\n",
        "tests/web/test_app.py": "from pkg import app\n\napp.config.debug\n",
        "tests/web/test_page.py": "import helpers\n",
        "tests/test_config.py": "import config\n",
    })
    sources = ["scripts", "tools", "pkg"]

    assert sweep.covering(root, "scripts/config.py", sources, "tests") == [
        os.path.join("tests", "test_config.py")]
    assert sweep.covering(root, "tools/helpers.py", sources, "tests") == []
    assert sweep.covering(root, "pkg/app.py", sources, "tests") == [
        os.path.join("tests", "web", "test_app.py")]


def test_a_file_under_pycache_in_the_tests_directory_is_not_coverage(tree):
    """Bytecode and stale copies are not what the project wrote."""
    write_tree(tree, {"pkg/tests/__pycache__/test_cached.py": "from pkg import idle\n"})

    assert sweep.covering(tree, _p("pkg/idle.py"), SOURCES, TESTS_DIR) == []


def test_a_harness_file_that_imports_a_module_does_not_cover_it(tree):
    """`helpers.py` imports both modules and holds no tests to run."""
    assert sweep.covering(tree, _p("pkg/gate.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_gate.py")]
    assert sweep.covering(tree, _p("loose/tool.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_tool.py")]


class _Engine:
    """Stands in for the engine's process, a `Popen`; `git` is still really
    run, through the real one."""

    def __init__(self, returncode=2, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.commands, self.calls = [], []
        self._real = sweep.subprocess.Popen

    def __call__(self, cmd, **kwargs):
        if cmd[0] == "git":
            return self._real(cmd, **kwargs)
        self.commands.append((cmd, kwargs))
        return self

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def wait(self, timeout=None):
        self.calls.append("wait")

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")


def test_the_driver_starts_the_engine_beside_it_with_the_stated_defaults(
        repo, monkeypatch):
    """A path to the engine that names nothing fails quietly: every module
    comes back without a score and the footer says none were measured.

    The defaults are the ones the README states.
    """
    engine = _Engine(stderr="refused: no mutation sites in x for RAISE")
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)

    assert sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                       "--modules", _p("pkg/gate.py")]) == 0

    ((cmd, kwargs),) = engine.commands
    assert cmd[0] == sweep.sys.executable
    assert os.path.isfile(cmd[1]) and os.path.basename(cmd[1]) == "mutate.py"
    assert cmd[2:8] == ["--target", _p("pkg/gate.py"), "--tests",
                        _p("pkg/tests/test_gate.py"), "--only", "RAISE"]
    assert cmd[8:10] == ["--limit", "25"]
    # The same directory, not the same text: git on Windows spells the
    # repository's path with forward slashes.
    assert os.path.samefile(kwargs["cwd"], repo)


def test_modules_with_nothing_after_it_sweeps_no_module(repo, monkeypatch,
                                                         capsys):
    """`--modules $(changed files)` with nothing changed is a request for no
    module, not for every one."""
    engine = _Engine()
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)

    assert sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                       "--modules"]) == 0

    assert engine.commands == []
    assert "0 module(s) measured; 0 with no test file" in capsys.readouterr().out


@pytest.mark.parametrize("said, rc, note", [
    ("refused: no mutation sites in pkg/gate.py for RAISE", 2,
     "no mutation sites"),
    ("refused: the selection is RED on the unmutated tree, so every mutant",
     2, "RED baseline"),
    ("Traceback (most recent call last):\n  ...\n"
     "During handling of the above exception, another exception occurred:\n"
     "ModuleNotFoundError: No module named 'pytest'", 1,
     "DRIVER FAILED rc=1: ModuleNotFoundError: No module named 'pytest'"),
    ("", 3, "DRIVER FAILED rc=3: no output"),
])
def test_a_module_with_no_score_says_why(repo, monkeypatch, tmp_path, said,
                                         rc, note):
    """An engine that could not start must not read as an answer about the
    module. The traceback says "occurred", which holds the letters "red".
    """
    monkeypatch.setattr(sweep.subprocess, "Popen", _Engine(rc, stderr=said))
    out = str(tmp_path / "sweep.json")

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR, "--json", out,
                "--modules", _p("pkg/gate.py")])

    with open(out, encoding="utf-8") as fh:
        (entry,) = json.load(fh)["measured"]
    assert entry["note"] == note and "score" not in entry


def test_a_real_sweep_separates_unmeasured_from_killed_and_survived(
        repo, tmp_path, capsys):
    """The whole path with nothing stubbed: the driver starts the engine.

    `idle.py` is imported by no test, and it must come back as unmeasured --
    a module with no covering test is not a module with a perfect score.
    """
    out = str(tmp_path / "sweep.json")

    assert sweep.main(["--src", *SOURCES, "--tests-dir", TESTS_DIR,
                       "--json", out]) == 0

    with open(out, encoding="utf-8") as fh:
        report = json.load(fh)
    assert report["unmeasured"] == [_p("pkg/idle.py"),
                                    _p("pkg/tests_extra/aid.py")]
    by = {r["module"]: r for r in report["measured"]}
    assert set(by) == {_p(m) for m in (
        "pkg/__init__.py", "pkg/gate.py", "pkg/sub/__init__.py",
        "pkg/sub/deep.py", "loose/tool.py")}

    gate = by[_p("pkg/gate.py")]
    assert (gate["killed"], gate["mutants"], gate["survivors"]) == (1, 1, [])
    assert gate["kills"][0]["killer"] == (
        "pkg/tests/test_gate.py::test_a_minor_is_refused")
    assert by[_p("pkg/sub/deep.py")]["score"] == 100.0

    tool = by[_p("loose/tool.py")]
    assert (tool["killed"], tool["mutants"]) == (0, 1)
    assert "SURVIVED" in tool["survivors"][0] and ":3" in tool["survivors"][0]

    # An empty `__init__.py` has tests but nothing to break, which is an
    # answer about the module and not a failure of the driver.
    assert by[_p("pkg/__init__.py")]["note"] == "no mutation sites"
    said = capsys.readouterr().out
    assert "3 module(s) measured; 2 with no test file importing them" in said


def test_the_sweep_s_json_carries_the_engine_s_own_report(repo, tmp_path):
    """A consumer that reads the sweep's file gets the survivors, accepted
    mutants and stale acceptances as the engine reports them, and not the
    printed line of each, which cannot be taken apart again.

    `tool.py` is given one of each: a survivor nothing accepts (the `<`),
    an accepted one (the `raise`), and an acceptance of a mutant its line
    does not have.
    """
    write_tree(repo, {"loose/tool.py": (
        "def tool(n):\n"
        "    if n < 0:\n"
        "        raise ValueError('negative')  # invective: accept[equivalent]"
        " no test asks\n"
        "    return n  # invective: accept[equivalent: 1 -> 3] no such mutant\n"
    )})
    out = str(tmp_path / "sweep.json")

    assert sweep.main(["--src", "loose", "--tests-dir", TESTS_DIR,
                       "--only", "RAISE,CMP", "--json", out]) == 0

    with open(out, encoding="utf-8") as fh:
        (entry,) = json.load(fh)["measured"]
    tool = _p("loose/tool.py")
    direct = mutate.mutate(repo, os.path.join(repo, tool), entry["tests"],
                           ["RAISE", "CMP"], 25, say=lambda line: None)
    # Each list is there to be non-empty: equal empty lists would pass for a
    # report that carried nothing.
    assert direct["survivors"] and direct["accepted"] and direct["stale"]
    got = entry["report"]
    assert got["target"] == entry["module"] == tool
    for kind in ("survivors", "accepted", "stale"):
        assert got[kind] == direct[kind]


@pytest.mark.parametrize("argv", [
    ["--src", "pkgg", "--tests-dir", TESTS_DIR],
    ["--src", "pkg", "--tests-dir", "pkg/test"],
])
def test_a_directory_that_is_not_there_is_refused(repo, capsys, argv):
    """A mistyped path walks as empty, and "0 module(s) measured" reads as a
    result."""
    assert sweep.main(argv) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_a_path_outside_the_project_is_refused(repo, capsys):
    """`..` from the top is a directory, and walked it would sweep whatever
    is above the project."""
    assert sweep.main(["--src", "..", "--tests-dir", TESTS_DIR]) == 2
    assert "is outside the project at" in capsys.readouterr().err


def test_a_report_the_engine_wrote_but_the_driver_cannot_read_is_said(
        repo, monkeypatch, capsys, tmp_path):
    """The score still comes from the printed summary; what only the report
    holds is unknown, not none."""
    monkeypatch.setattr(sweep.subprocess, "Popen", _Engine(
        0, stdout="1/1 killed (100.0%), 0 survived\n"))
    out = str(tmp_path / "sweep.json")

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR, "--json", out,
                "--modules", _p("pkg/gate.py")])

    with open(out, encoding="utf-8") as fh:
        (entry,) = json.load(fh)["measured"]
    assert (entry["score"], entry["kills"], entry["broken"]) == (100.0, None, None)
    assert entry["report"] is None
    assert "(could not read %s's own report)" % _p("pkg/gate.py") in (
        capsys.readouterr().out)


@pytest.mark.parametrize("argv, missing", [
    (["--tests-dir", TESTS_DIR], "--src"),
    (["--src", "pkg"], "--tests-dir"),
])
def test_the_command_says_which_argument_is_missing(capsys, argv, missing):
    with pytest.raises(SystemExit) as stopped:
        sweep.main(argv)
    assert stopped.value.code == 2
    assert missing in capsys.readouterr().err


def test_the_sweep_command_line_is_one_a_test_can_parse():
    """`parser()` is `main`'s own parser, built apart from it so a check of
    the documented commands can parse them without running anything."""
    args = sweep.parser().parse_args(
        ["--src", "pkg", "loose", "--tests-dir", TESTS_DIR,
         "--modules", "pkg/gate.py", "loose/idle.py"])

    assert (args.src, args.tests_dir, args.modules) == (
        ["pkg", "loose"], TESTS_DIR, ["pkg/gate.py", "loose/idle.py"])


def test_the_help_says_what_modules_does(capsys):
    with pytest.raises(SystemExit) as stopped:
        sweep.main(["--help"])
    assert stopped.value.code == 0
    # argparse wraps the help to the terminal's width.
    out = " ".join(capsys.readouterr().out.split())
    assert "--modules" in out
    assert "sweep only these module files" in out
    assert "instead of discovering them under --src" in out


def test_a_module_that_breaks_the_project_s_rules_fails_the_sweep(
        repo, monkeypatch, capsys):
    monkeypatch.setattr(sweep.subprocess, "Popen", _Engine(
        1, stdout=("1/2 killed (50.0%), 1 survived\n"
                   "fails:     1 survivor(s) that no comment accepts\n")))

    assert sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                       "--modules", _p("pkg/gate.py")]) == 1
    said = capsys.readouterr().out
    assert "fails: 1 survivor(s) that no comment accepts" in said
    assert "1 module(s) break the project's rules: %s" % _p("pkg/gate.py") in said


def test_a_ref_given_to_the_sweep_reaches_the_engine(repo, monkeypatch):
    engine = _Engine(0, stdout="1/1 killed (100.0%), 0 survived\n")
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR, "--ref", "main",
                "--modules", _p("pkg/gate.py")])

    ((cmd, _kwargs),) = engine.commands
    assert cmd[cmd.index("--ref") + 1] == "main"


def test_a_terminated_sweep_terminates_the_engine_it_started(repo, monkeypatch):
    """`subprocess.run` kills its child outright on any exception, with the
    engine's copy stranded. The engine is told to stop instead and waited
    for, and the sweep then dies by the signal, as `run` does."""
    engine = _Engine()

    def communicate(timeout=None):
        raise mutate._Terminated(signal.SIGTERM)

    engine.communicate = communicate
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)
    died = []
    monkeypatch.setattr(sweep, "_exit_by", lambda exc: died.append(exc.signum))

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                "--modules", _p("pkg/gate.py")])

    assert engine.calls == ["terminate", "wait"]
    assert died == [signal.SIGTERM]


@pytest.mark.skipif(os.name == "nt", reason="Windows never delivers SIGTERM")
def test_a_sweep_sent_sigterm_stops_its_engine_and_dies_by_the_signal(
        tmp_path):
    """The whole path, with the signal really delivered: the handler is
    installed, the engine is told and waited for, its run's group is gone
    and its copy removed, and only then does the sweep exit by the signal."""
    # A project of its own: the fixture's tests would kill the mutant before
    # the slow one ran.
    root = str(tmp_path / "proj")
    pid_file = str(tmp_path / "run.pid")
    files = {rel: text for rel, text in FILES.items()
             if rel not in ("pkg/tests/test_gate.py", "pkg/tests/helpers.py")}
    write_tree(root, {**files, "pyproject.toml": ("[project]\nname = 'x'\n"
                                          "[tool.pytest.ini_options]\n"),
                      "pkg/tests/test_slow.py": SLOW_TEST % pid_file})
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "invective", "sweep", "--src", "pkg",
         "--tests-dir", "pkg/tests", "--only", "RAISE",
         "--modules", "pkg/gate.py"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": SRC})
    run = None
    try:
        run = int(wait_for(pid_file))
        sent = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)

        assert proc.wait(timeout=60) == -signal.SIGTERM
        # The engine was told at once. Left to the escalation, which waits
        # out the whole grace first, the sweep would still end the same way.
        assert time.monotonic() - sent < sweep._GRACE
        with pytest.raises(ProcessLookupError):
            os.killpg(run, 0)
        assert [n for n in os.listdir(tmp_path)
                if n.startswith("invective-")] == []
    finally:
        if proc.poll() is None:
            proc.kill()
        if run is not None:
            stop_group(run)
        proc.communicate()


def test_an_engine_that_outlasts_its_grace_is_told_again_and_then_killed(
        repo, monkeypatch):
    """An engine that does not end within the grace is terminated once more
    (its handler makes that harmless when it was told already), given the
    grace again, and only then killed outright and waited for."""
    engine = _Engine()
    waits = []

    def wait(timeout=None):
        engine.calls.append("wait")
        waits.append(timeout)
        if len(waits) <= 2:
            raise subprocess.TimeoutExpired("engine", timeout)

    def communicate(timeout=None):
        raise mutate._Terminated(signal.SIGTERM)

    engine.wait, engine.communicate = wait, communicate
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)
    monkeypatch.setattr(sweep, "_exit_by", lambda exc: None)
    monkeypatch.setattr(sweep, "_GRACE", 0.01)

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                "--modules", _p("pkg/gate.py")])

    assert engine.calls == ["terminate", "wait", "terminate", "wait",
                            "kill", "wait"]
    assert waits == [0.01, 0.01, None]


@pytest.mark.parametrize("raised, told", [
    (KeyboardInterrupt, []),
    (RuntimeError, ["terminate"]),
])
def test_an_interrupted_sweep_waits_for_the_engine_and_the_interrupt_goes_on(
        repo, monkeypatch, raised, told):
    """A ^C reached the engine too, in the same foreground group, so it is
    only waited for: told to stop as well, its own cleanup would be cut
    short. Anything else the engine has not heard of. Either way the
    exception comes out once the engine is gone, as it would have."""
    engine = _Engine()

    def communicate(timeout=None):
        raise raised()

    engine.communicate = communicate
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)

    with pytest.raises(raised):
        sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                    "--modules", _p("pkg/gate.py")])
    assert engine.calls == told + ["wait"]


def test_the_sweep_runs_from_a_subdirectory_of_the_project(tree, monkeypatch,
                                                           tmp_path):
    """From `pkg` the engine is started at the project's top, given the
    module from there. Started in `pkg`, it copies `pkg` alone, which holds
    no `pkg` to import, and the baseline is red."""
    monkeypatch.chdir(os.path.join(tree, "pkg"))
    out = str(tmp_path / "sweep.json")

    assert sweep.main(["--src", ".", "--tests-dir", "tests", "--modules",
                       "gate.py", "--json", out]) == 0

    with open(out, encoding="utf-8") as fh:
        (entry,) = json.load(fh)["measured"]
    assert entry["module"] == _p("pkg/gate.py")
    assert entry["tests"] == [_p("pkg/tests/test_gate.py")]
    assert (entry["killed"], entry["mutants"]) == (1, 1)


def test_a_sweep_whose_pytest_settings_are_above_the_project_is_refused(
        tmp_path, monkeypatch, capsys):
    """Said once, before any module is measured under other settings."""
    top = os.path.realpath(tmp_path / "ws")
    write_tree(top, WORKSPACE)
    monkeypatch.chdir(os.path.join(top, "m"))

    assert sweep.main(["--src", "pkg", "--tests-dir", "tests"]) == 2
    said = capsys.readouterr()
    assert "refused: pytest reads %s" % os.path.join(top, "pyproject.toml") in (
        said.err)
    assert "measured" not in said.out
