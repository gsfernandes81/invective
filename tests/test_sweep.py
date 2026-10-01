"""The sweep: which modules it mutates, and which tests it runs against each."""

from __future__ import annotations

import json
import os

import pytest

from invective import sweep

from conftest import write_tree

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


def test_a_harness_file_that_imports_a_module_does_not_cover_it(tree):
    """`helpers.py` imports both modules and holds no tests to run."""
    assert sweep.covering(tree, _p("pkg/gate.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_gate.py")]
    assert sweep.covering(tree, _p("loose/tool.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_tool.py")]


class _Engine:
    """Stands in for the engine's process; `git` is still really run."""

    def __init__(self, returncode=2, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.commands = []
        self._real = sweep.subprocess.run

    def __call__(self, cmd, **kwargs):
        if cmd[0] == "git":
            return self._real(cmd, **kwargs)
        self.commands.append((cmd, kwargs))
        return self


def test_the_driver_starts_the_engine_beside_it_with_the_stated_defaults(
        repo, monkeypatch):
    """A path to the engine that names nothing fails quietly: every module
    comes back without a score and the footer says none were measured.

    The defaults are the ones the README states.
    """
    engine = _Engine(stderr="refused: no mutation sites in x for RAISE")
    monkeypatch.setattr(sweep.subprocess, "run", engine)

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
    monkeypatch.setattr(sweep.subprocess, "run", _Engine(rc, stderr=said))
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


@pytest.mark.parametrize("argv", [
    ["--src", "pkgg", "--tests-dir", TESTS_DIR],
    ["--src", "pkg", "--tests-dir", "pkg/test"],
])
def test_a_directory_that_is_not_there_is_refused(repo, capsys, argv):
    """A mistyped path walks as empty, and "0 module(s) measured" reads as a
    result."""
    assert sweep.main(argv) == 2
    assert "is not a directory" in capsys.readouterr().err
