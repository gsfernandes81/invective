"""`pytest --mutate`: the plugin, driven by pytest itself in a real process."""

from __future__ import annotations

import importlib.util
import json
import logging.handlers
import os
import queue
import re
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from conftest import (FILES, MARKERLESS, WORKSPACE, commit,
                      no_pytest_settings_above, write_tree)

#: This checkout's own `src`, ahead of any installed copy, so that the pytest
#: started here loads the plugin under test.
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")

MINOR = "pkg/tests/test_gate.py::test_a_minor_is_refused"
ADULT = "pkg/tests/test_gate.py::test_an_adult_is_admitted"


def _p(rel):
    return os.path.join(*rel.split("/"))


def pytest_in(repo, *args, env=None, terminal=True):
    # `--color` is the terminal plugin's own option.
    colour = ["--color=no"] if terminal else ["-p", "no:terminal"]
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *colour,
         *args],
        cwd=repo, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONPATH": SRC, **(env or {})})


def test_each_module_is_measured_against_the_collected_tests(repo, tmp_path):
    """Two modules, one selection, a report for each."""
    out = tmp_path / "reports.json"

    done = pytest_in(repo, "--mutate", "pkg/gate.py", "--mutate",
                     "pkg/sub/deep.py", "--mutate-only", "raise,bool",
                     "--mutate-json", str(out), "pkg/tests/test_gate.py",
                     "pkg/tests/test_deep_things.py")

    assert done.returncode == 0, done.stdout + done.stderr
    gate, deep = json.loads(out.read_text(encoding="utf-8"))
    assert gate["tests"] == deep["tests"] == [
        MINOR, ADULT, "pkg/tests/test_deep_things.py::test_nothing_is_refused"]
    assert gate["target"] == _p("pkg/gate.py")
    assert (gate["mutants"], gate["killed"]) == (2, 1)
    assert [k["killer"] for k in gate["kills"]] == [MINOR]
    assert [s["line"] for s in gate["survivors"]] == [4]
    assert (deep["mutants"], deep["killed"]) == (1, 1)
    assert "invective: %s" % _p("pkg/gate.py") in done.stdout
    assert "1/2 killed (50.0%), 1 survived" in done.stdout


def test_the_mutants_meet_only_the_tests_pytest_selected(repo, tmp_path):
    """`-k adult` leaves out the test of the refusal, so the refusal's mutant
    survives: the selection is what pytest collected, not the file named."""
    out = tmp_path / "reports.json"

    done = pytest_in(repo, "--mutate", "pkg/gate.py", "--mutate-only", "RAISE",
                     "--mutate-json", str(out), "-k", "adult",
                     "pkg/tests/test_gate.py")

    assert done.returncode == 0, done.stdout + done.stderr
    (gate,) = json.loads(out.read_text(encoding="utf-8"))
    assert gate["tests"] == [ADULT]
    assert (gate["mutants"], gate["killed"]) == (1, 0)


def test_a_selection_of_nothing_is_refused_and_runs_nothing(repo):
    """Handed an empty selection, a run would collect every test it found."""
    done = pytest_in(repo, "--mutate", "pkg/gate.py", "-k", "no_test_has_this",
                     "pkg/tests/test_gate.py")

    assert done.returncode == 2
    assert "refused: pytest collected no tests" in done.stdout
    assert "copy:" not in done.stdout


def test_a_red_baseline_is_refused(repo):
    commit(repo, {"pkg/tests/test_red.py": "def test_red():\n    assert False\n"})

    done = pytest_in(repo, "--mutate", "pkg/gate.py", "pkg/tests/test_red.py")

    assert done.returncode == 2
    assert "is RED on the unmutated tree" in done.stdout


@pytest.mark.parametrize("carry_on", [[], ["--continue-on-collection-errors"]])
def test_a_collection_error_stops_the_run_before_any_mutant(repo, carry_on):
    """The tests that did collect are not the selection that was asked for,
    even when pytest is told to carry on without the rest."""
    write_tree(repo, {"pkg/tests/test_broken.py": "def broken(:\n"})

    done = pytest_in(repo, *carry_on, "--mutate", "pkg/gate.py", "pkg/tests")

    assert done.returncode == 2
    assert "refused: 1 error(s) during collection" in done.stdout
    assert "copy:" not in done.stdout


def test_node_ids_are_given_from_the_top_of_the_repository(repo, tmp_path):
    """A configuration file in `pkg` makes it pytest's root, so pytest's ids
    start at `tests/`; each run starts at the top of the worktree, so the ids
    it is handed, and the killer it hands back, start at `pkg/`.
    """
    commit(repo, {"pkg/pytest.ini": "[pytest]\n"})
    out = tmp_path / "reports.json"

    done = pytest_in(repo, "--mutate", "pkg/gate.py", "--mutate-only", "RAISE",
                     "--mutate-json", str(out), "pkg/tests/test_gate.py")

    assert done.returncode == 0, done.stdout + done.stderr
    (gate,) = json.loads(out.read_text(encoding="utf-8"))
    assert gate["tests"] == [MINOR, ADULT]
    assert [k["killer"] for k in gate["kills"]] == [MINOR]


def test_a_test_whose_path_is_not_ascii_is_found(repo, tmp_path):
    """The selection is read back by argparse, in its own choice of encoding."""
    commit(repo, {"pkg/tests/test_größe.py": (
        "import pytest\n"
        "from pkg import gate\n"
        "\n"
        "def test_a_minor_is_refused():\n"
        "    with pytest.raises(ValueError):\n"
        "        gate.admit(10, False)\n")})
    out = tmp_path / "reports.json"

    done = pytest_in(repo, "--mutate", "pkg/gate.py", "--mutate-only", "RAISE",
                     "--mutate-json", str(out), "pkg/tests/test_größe.py")

    assert done.returncode == 0, done.stdout + done.stderr
    (gate,) = json.loads(out.read_text(encoding="utf-8"))
    assert [k["killer"] for k in gate["kills"]] == [
        "pkg/tests/test_größe.py::test_a_minor_is_refused"]


def test_a_test_outside_the_project_is_refused(repo, tmp_path):
    """No run at the last commit has it."""
    # pytest starts its search above the project, where the test is.
    no_pytest_settings_above(tmp_path)
    outside = tmp_path / "outside" / "test_outside.py"
    outside.parent.mkdir()
    outside.write_text("def test_outside():\n    pass\n", encoding="utf-8")

    done = pytest_in(repo, "--mutate", "pkg/gate.py", str(outside))

    assert done.returncode == 2
    assert "is outside the project" in done.stdout
    assert "copy:" not in done.stdout


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="a symbolic link without privileges")
def test_a_test_typed_through_a_link_to_the_project_is_the_project_s(
        tree, tmp_path):
    """Given an absolute path through a link to the project, as every one is
    from a logical `$PWD` entered through it, pytest keeps the test's path
    as typed while the project's top is the real directory: the test is the
    project's own, given from its top, and not one outside it."""
    link = tmp_path / "link"
    os.symlink(tree, link)

    done = pytest_in(str(link), "--mutate=pkg/gate.py", "--mutate-only",
                     "RAISE", str(link / "pkg" / "tests" / "test_gate.py"))

    assert done.returncode == 0, done.stdout + done.stderr
    assert "1/1 killed (100.0%)" in done.stdout


def test_mutate_from_a_subdirectory_copies_the_whole_project(repo, tmp_path):
    """Started in `pkg`, the run copies the project from its top. A copy of
    `pkg` alone holds no `pkg` to import, so its baseline is red.

    The project's `pythonpath` puts its top on `sys.path`, which `python -m`
    does only for the directory pytest is started in.
    """
    write_tree(repo, {"pyproject.toml":
                      "[tool.pytest.ini_options]\npythonpath = ['.']\n"})
    out = tmp_path / "reports.json"

    done = pytest_in(os.path.join(repo, "pkg"), "--mutate", "gate.py",
                     "--mutate-only", "RAISE", "--mutate-json", str(out),
                     "tests/test_gate.py")

    assert done.returncode == 0, done.stdout + done.stderr
    (gate,) = json.loads(out.read_text(encoding="utf-8"))
    assert gate["target"] == _p("pkg/gate.py")
    assert gate["tests"] == [MINOR, ADULT]
    assert (gate["mutants"], gate["killed"]) == (1, 1)


def test_a_project_without_a_marker_inside_a_repository_is_its_own_top(
        tmp_path):
    """A repository's directory is no project's top by itself: a service of
    a monorepo with no marker is copied from where pytest was started, where
    its tests import it, and not as the whole repository."""
    no_pytest_settings_above(tmp_path)
    mr = os.path.realpath(tmp_path / "mr")
    write_tree(mr, {".git/HEAD": "ref: refs/heads/main\n", **MARKERLESS})
    api = os.path.join(mr, "api")

    done = pytest_in(api, "--mutate", "app/gate.py", "--mutate-only",
                     "RAISE", "tests")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "project:   %s" % api in done.stdout
    assert "1/1 killed (100.0%)" in done.stdout


def test_mutate_whose_pytest_settings_are_above_the_project_is_refused(
        tmp_path):
    """This pytest reads the workspace's settings, a copy of the member `m`
    would not have them, and the mutant only they kill would survive. From
    the top, the copy holds them."""
    top = os.path.realpath(tmp_path / "ws")
    write_tree(top, WORKSPACE)

    done = pytest_in(os.path.join(top, "m"), "--mutate", "pkg/gate.py",
                     "--mutate-only", "RAISE", "tests/test_gate.py")

    assert done.returncode == 2, done.stdout + done.stderr
    assert "refused: pytest reads %s, which is above the project's top" % (
        os.path.join(top, "pyproject.toml")) in done.stdout
    assert "copy:" not in done.stdout

    done = pytest_in(top, "--mutate", "m/pkg/gate.py", "--mutate-only",
                     "RAISE", "m/tests/test_gate.py")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1/1 killed (100.0%)" in done.stdout


def test_a_settings_file_given_outside_the_project_is_refused(tree, tmp_path):
    """`-c` skips pytest's search, so a file outside the project given with
    it is what this run goes by, even when it sets nothing; no copy holds
    it, and every mutant's run would go by the project's own settings."""
    given = tmp_path / "elsewhere" / "pytest.ini"
    write_tree(str(tmp_path), {"elsewhere/pytest.ini": "[pytest]\n"})

    done = pytest_in(tree, "-c", str(given), "--mutate=pkg/gate.py",
                     "--mutate-only", "RAISE", "pkg/tests/test_gate.py")

    assert done.returncode == 2, done.stdout + done.stderr
    assert ("refused: pytest's settings file %s, given with -c, is outside "
            "the project at %s" % (given, tree)) in done.stdout
    assert "copy:" not in done.stdout


def test_a_settings_file_found_above_the_project_that_sets_nothing_is_let_be(
        tree, tmp_path):
    """pytest's own search stops above the project only when the project
    has no settings of its own, and every run in the copy finds none there
    either: a file up there that sets nothing changes nothing, unlike one
    given with `-c`."""
    # The fixture's own settings table would stop the search at the tree.
    write_tree(tree, {"pyproject.toml": ""})
    write_tree(str(tmp_path), {"pytest.ini": "[pytest]\n"})

    done = pytest_in(tree, "--mutate=pkg/gate.py", "--mutate-only", "RAISE",
                     "pkg/tests/test_gate.py")

    assert "rootdir: %s\nconfigfile: pytest.ini\n" % os.path.realpath(
        tmp_path) in done.stdout
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1/1 killed (100.0%)" in done.stdout


def test_settings_above_the_copy_of_a_project_with_none_are_refused(
        settings_above_the_copy, tmp_path):
    """pytest read no settings file here, so it has none to name to the
    runs, and each would read the one above the temporary directory the
    copy is in: the run is refused, naming it, before any test runs."""
    proj = os.path.realpath(tmp_path / "proj")
    write_tree(proj, {rel: text for rel, text in FILES.items()
                      if rel != "pyproject.toml"})
    os.mkdir(os.path.join(proj, ".git"))

    done = pytest_in(proj, "--mutate=pkg/gate.py", "--mutate-only", "RAISE",
                     "pkg/tests/test_gate.py")

    assert done.returncode == 2, done.stdout + done.stderr
    assert "refused: pytest reads %s, above the copy" % os.path.join(
        str(tmp_path), "above", "pytest.ini") in done.stdout
    assert not settings_above_the_copy.exists()


#: A project whose pytest settings sit below its top, in `tests/`: pytest
#: started with `tests` as its path reads them, and only they make the
#: expected failure strict, so only they tell the `RAISE` mutant apart.
BELOW_THE_TOP = {
    "pyproject.toml": "[project]\nname = 'p'\nversion = '0'\n",
    "pkg/__init__.py": "",
    "pkg/gate.py": ("def check(x):\n"
                    "    if x < 0:\n"
                    "        raise ValueError('neg')\n"
                    "    return x\n"),
    "tests/pytest.ini": "[pytest]\nxfail_strict = true\npythonpath = ..\n",
    "tests/test_gate.py": ("import pytest\n"
                           "from pkg.gate import check\n"
                           "\n"
                           "@pytest.mark.xfail(raises=ValueError)\n"
                           "def test_neg():\n"
                           "    check(-1)\n"),
}


@pytest.mark.parametrize("given", [[], ["-c", _p("tests/pytest.ini")]],
                         ids=["found", "given"])
def test_settings_below_the_top_reach_every_mutant_s_run(tmp_path, given):
    """Each run starts at the top of the copy with only node ids to go by,
    so without the file this pytest read it would read the top's
    `pyproject.toml`, the xfail would not be strict, and the mutant would
    survive.

    `--mutate=pkg/gate.py` and not `--mutate pkg/gate.py`: pytest settles
    its settings before it knows the plugin's options, so it takes a
    separate `pkg/gate.py` for a path to test, searches from the directory
    above both paths, and reads the top's `pyproject.toml` itself."""
    project = os.path.realpath(tmp_path / "proj")
    write_tree(project, BELOW_THE_TOP)

    done = pytest_in(project, *given, "--mutate=pkg/gate.py",
                     "--mutate-only", "RAISE", "tests")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "1/1 killed" in done.stdout


def test_mutate_inside_a_mutant_s_own_run_is_a_usage_error(repo, tmp_path):
    """In PYTEST_ADDOPTS or addopts, `--mutate` reaches every mutant's run,
    each would start a campaign of its own, and so on without end. A run that
    was handed a verdict file is a mutant's run, so it refuses.

    The verdict variable is given here directly rather than by letting the
    options be inherited: without the refusal, a test that did that would
    never end, and neither would the processes it started.
    """
    done = pytest_in(repo, "--mutate", "pkg/gate.py", "pkg/tests/test_gate.py",
                     env={"INVECTIVE_VERDICT": str(tmp_path / "verdict.json")})

    assert done.returncode == 4
    assert "--mutate reached a mutant's own run" in done.stderr


def test_without_the_terminal_the_score_is_still_said(repo):
    done = pytest_in(repo, "--mutate", "pkg/gate.py", "--mutate-only", "RAISE",
                     "pkg/tests/test_gate.py", terminal=False)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "1/1 killed (100.0%), 0 survived" in done.stdout


def test_collect_only_lists_the_tests_and_mutates_nothing(repo):
    done = pytest_in(repo, "--collect-only", "--mutate", "pkg/gate.py",
                     "pkg/tests/test_gate.py")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "test_a_minor_is_refused" in done.stdout
    assert "copy:" not in done.stdout


def test_without_mutate_the_plugin_leaves_the_run_alone(repo):
    done = pytest_in(repo, "pkg/tests/test_gate.py")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "2 passed" in done.stdout
    # pytest's header lists the installed plugins, in an order that is the
    # site directory's and not alphabetical, so the whole line goes. The
    # fixture's path goes too, from the `rootdir:` line: a temporary
    # directory under one named for invective would say it.
    said = re.sub(r"(?m)^plugins: .*$", "", done.stdout)
    said = said.replace(repo, "")
    assert "invective" not in said


def test_a_kind_of_edit_that_does_not_exist_is_a_usage_error(repo):
    done = pytest_in(repo, "--mutate", "pkg/gate.py", "--mutate-only",
                     "CMP,RISE", "pkg/tests/test_gate.py")

    assert done.returncode == 4
    assert "unknown kind(s) of edit: RISE" in done.stderr


def test_workers_are_a_usage_error(repo):
    """Under pytest-xdist the controlling process collects nothing."""
    done = pytest_in(repo, "-n", "2", "--mutate", "pkg/gate.py",
                     "pkg/tests/test_gate.py")

    assert done.returncode == 4
    assert "-n 0" in done.stderr


def test_the_verdict_is_the_first_test_to_fail(tmp_path):
    """The verdict's contract, whatever the run's other options: each
    mutant's run is given `-x` and so stops at its first failure, and the
    verdict must not lean on that."""
    (tmp_path / "test_two.py").write_text(
        "def test_first():\n    assert False\n\n"
        "def test_second():\n    assert False\n", encoding="utf-8")
    verdict = tmp_path / "verdict.json"

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-n", "0",
         "-p", "pytest_invective", "test_two.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": SRC,
             "INVECTIVE_VERDICT": str(verdict)})

    assert done.returncode == 1, done.stdout + done.stderr
    assert json.loads(verdict.read_text(encoding="utf-8")) == {
        "killer": "test_two.py::test_first", "missing": [], "elsewhere": ""}


#: A package the tests import, as a project lays one out under `src/`.
PKG = {"src/pkg/__init__.py": "",
       "src/pkg/gate.py": FILES["pkg/gate.py"]}

#: Passes, and imports nothing of the project's.
IDLE_TEST = {"tests/test_idle.py": "def test_idle():\n    pass\n"}


def _elsewhere(tmp_path, copy, target, *on_path, extra=()):
    """The verdict's `elsewhere` for a run in `copy/` with *target* given as
    the mutated module, *on_path* joined to this checkout's `src` as the
    run's `PYTHONPATH`, the plugin asked for as the engine asks for it."""
    where = os.path.realpath(tmp_path / "copy")
    write_tree(where, copy)
    verdict = tmp_path / "verdict.json"
    done = pytest_in(
        where, "-n", "0", "-p", "pytest_invective", *extra, "tests",
        env={"PYTHONPATH": os.pathsep.join([SRC, *on_path]),
             "INVECTIVE_VERDICT": str(verdict), "INVECTIVE_TARGET": target})
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(verdict.read_text(encoding="utf-8"))["elsewhere"]


def _file(tmp_path, rel):
    # As the filesystem spells it, which is how the verdict names a file:
    # `normcase` would lowercase it on Windows.
    return os.path.realpath(tmp_path / _p(rel))


def test_the_verdict_names_the_target_when_it_was_loaded_elsewhere(tmp_path):
    """Two trees with the package, and the other one's `src` on the path:
    the tests import `pkg.gate` from there, and the verdict says so, naming
    the file. The copy's `__init__.py` is what gives the target that name;
    without it the target would be a loose `gate`."""
    write_tree(tmp_path / "other", PKG)

    assert _elsewhere(
        tmp_path, {**PKG, "tests/test_gate.py": FILES["pkg/tests/test_gate.py"]},
        "src/pkg/gate.py", str(tmp_path / "other" / "src"),
    ) == _file(tmp_path, "other/src/pkg/gate.py")


def test_the_plugin_itself_is_never_the_module_loaded_elsewhere(tmp_path):
    """pytest loads the plugin from wherever invective is installed, before
    any conftest could redirect it, and in a run of invective on its own
    code that is the checkout: its mutants are seen by the tests that start
    pytest afresh, and a check that named it would refuse that run."""
    assert _elsewhere(
        tmp_path, {"src/pytest_invective/__init__.py": "", **IDLE_TEST},
        "src/pytest_invective/__init__.py") == ""


def test_a_module_loaded_before_the_plugin_is_never_the_one_named(tmp_path):
    """`platform` is in `sys.modules` before the plugin is imported, from
    the standard library, under exactly the name the copy gives this loose
    file, and the tests never load it. The library's file is not where the
    tests got the target from."""
    assert _elsewhere(
        tmp_path, {"tools/platform.py": "def check(n):\n    return n\n",
                   **IDLE_TEST},
        "tools/platform.py") == ""


def test_a_target_a_plugin_in_addopts_loaded_from_elsewhere_is_still_named(
        tmp_path):
    """A plugin in the project's `addopts` is imported before invective's,
    so what it loaded is among the modules loaded before the plugin, and
    would be let be as the library's are. Loaded at the target's own layout
    outside the copy, `pkg/gate.py`, it is the file the tests got."""
    write_tree(tmp_path / "other", {**PKG, "src/early.py": "import pkg.gate\n"})

    assert _elsewhere(
        tmp_path, {**PKG, **IDLE_TEST,
                   "pytest.ini": "[pytest]\naddopts = -p early\n"},
        "src/pkg/gate.py", str(tmp_path / "other" / "src"),
    ) == _file(tmp_path, "other/src/pkg/gate.py")


def test_a_package_s_own_init_loaded_from_elsewhere_is_refused(tmp_path):
    """The target is `src/pkg/__init__.py`, which is `pkg` itself and not a
    module inside it. The tests import `pkg` from the other tree's `src`, on
    the path as an editable install's `.pth` line would put it, and the run
    is refused naming that tree's file."""
    package = {"src/pkg/__init__.py": ("def admit(age):\n"
                                       "    if age < 18:\n"
                                       "        raise ValueError(age)\n"
                                       "    return True\n")}
    write_tree(tmp_path / "other", package)
    project = os.path.realpath(tmp_path / "project")
    write_tree(project, {**package,
                         "pyproject.toml": "[tool.pytest.ini_options]\n",
                         "tests/test_pkg.py": (
                             "import pytest\n"
                             "from pkg import admit\n"
                             "\n"
                             "def test_a_minor():\n"
                             "    with pytest.raises(ValueError):\n"
                             "        admit(17)\n")})

    done = pytest_in(project, "--mutate", "src/pkg/__init__.py",
                     "tests/test_pkg.py",
                     env={"PYTHONPATH": os.pathsep.join(
                         [SRC, str(tmp_path / "other" / "src")])})

    assert done.returncode == 2, done.stdout + done.stderr
    assert "refused: " in done.stdout
    assert " was imported from %s, not from the copy" % _file(
        tmp_path, "other/src/pkg/__init__.py") in done.stdout


#: The target's package as the copy holds it in the cases below, which call
#: `_loaded_elsewhere` in this process with modules made up for them.
NESTED = {"src/pkg/__init__.py": "", "src/pkg/sub/__init__.py": "",
          "src/pkg/sub/mod.py": "def f():\n    return 1\n"}
MOD = "src/pkg/sub/mod.py"
#: The other tree's copy of the target.
OTHER_MOD = "other/src/pkg/sub/mod.py"
#: A module of the other tree under a name the target could have, but not
#: at the target's layout, `pkg/sub/mod.py`.
NOT_AT_LAYOUT = "other/lib/sub/mod.py"

#: The names those modules are put under, emptied first, so that nothing
#: this process holds answers for them.
NAMES = ("pkg", "pkg.sub", "pkg.sub.mod", "sub", "sub.mod", "mod",
         "packaging")


def _decide(tmp_path, monkeypatch, target, loaded, before=(), copy=NESTED,
            library=None):
    """`_loaded_elsewhere` of this checkout's plugin for a copy at `copy/`
    holding *copy*, with `other/` beside it holding the same package, and
    with *loaded* in `sys.modules`: name -> the module's `__file__`, given
    from *tmp_path* unless it is None (a module with no `__file__`) or
    absolute. The plugin takes *before* for the modules loaded ahead of it,
    and *library*, when given, for the interpreter's own library."""
    plugin = _plugin_here()
    monkeypatch.setattr(plugin, "_LOADED_BEFORE", frozenset(before))
    if library is not None:
        # Through `normcase`, as the plugin's own `_LIBRARY` is.
        monkeypatch.setattr(plugin, "_LIBRARY", (
            os.path.join(os.path.normcase(_file(tmp_path, library)), ""),))
    write_tree(tmp_path / "copy", copy)
    write_tree(tmp_path / "other", {**NESTED, "lib/sub/mod.py": ""})
    for name in NAMES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name, file in loaded.items():
        module = ModuleType(name)
        if file is not None:
            module.__file__ = (file if os.path.isabs(file)
                               else str(tmp_path / _p(file)))
        monkeypatch.setitem(sys.modules, name, module)
    return plugin._loaded_elsewhere(str(tmp_path / "copy"), target)


def test_the_name_loaded_from_another_tree_is_that_tree_s_file(
        tmp_path, monkeypatch):
    """The name is `pkg.sub.mod`, the outermost package first."""
    assert _decide(tmp_path, monkeypatch, MOD, {"pkg.sub.mod": OTHER_MOD}) \
        == _file(tmp_path, OTHER_MOD)


def test_a_module_that_fails_on_its_file_is_not_the_one_named(
        tmp_path, monkeypatch):
    """A lazily loaded module under the target's name can fail on any
    attribute; the verdict does not turn into the plugin's own error."""
    class Lazy(ModuleType):
        def __getattr__(self, name):
            raise RuntimeError("the load failed")

    plugin = _plugin_here()
    monkeypatch.setattr(plugin, "_LOADED_BEFORE", frozenset())
    write_tree(tmp_path / "copy", NESTED)
    monkeypatch.setitem(sys.modules, "pkg.sub.mod", Lazy("pkg.sub.mod"))

    assert plugin._loaded_elsewhere(str(tmp_path / "copy"), MOD) == ""


@pytest.mark.parametrize("file", [OTHER_MOD + "c", None],
                         ids=["pyc", "no-file"])
def test_a_module_with_no_source_file_is_never_the_one_named(
        tmp_path, monkeypatch, file):
    """A `.pyc`, or a module with no `__file__`, is not a file a mutant
    could be in."""
    assert _decide(tmp_path, monkeypatch, MOD, {"pkg.sub.mod": file}) == ""


def test_a_relative_file_is_read_from_the_top_of_the_copy(
        tmp_path, monkeypatch):
    """A relative `sys.path` entry gives a relative `__file__`, relative to
    where the run started, which is the top of the copy: the copy's own
    file, whatever this process's directory, here the other tree."""
    write_tree(tmp_path / "other", NESTED)
    monkeypatch.chdir(tmp_path / "other")
    plugin = _plugin_here()
    monkeypatch.setattr(plugin, "_LOADED_BEFORE", frozenset())
    write_tree(tmp_path / "copy", NESTED)
    module = ModuleType("pkg.sub.mod")
    module.__file__ = _p(MOD)
    monkeypatch.setitem(sys.modules, "pkg.sub.mod", module)

    assert plugin._loaded_elsewhere(str(tmp_path / "copy"), MOD) == ""


@pytest.mark.parametrize("loaded, named", [
    ({"sub.mod": OTHER_MOD}, OTHER_MOD),
    ({"mod": OTHER_MOD}, OTHER_MOD),
    ({"sub.mod": NOT_AT_LAYOUT}, None),
    ({"sub.mod": "copy/" + MOD}, None),
], ids=["sub.mod", "mod", "not-at-layout", "in-the-copy"])
def test_under_a_shorter_name_only_the_target_s_layout_elsewhere_is_named(
        tmp_path, monkeypatch, loaded, named):
    """With nothing under `pkg.sub.mod`, each shorter name is looked up,
    `sub.mod` then `mod`, and its module is the target only at
    `.../pkg/sub/mod.py` outside the copy."""
    assert _decide(tmp_path, monkeypatch, MOD, loaded) == (
        _file(tmp_path, named) if named else "")


def test_a_shorter_name_loaded_before_the_plugin_is_let_be(
        tmp_path, monkeypatch):
    """Even at the target's layout: only the target's own name has the
    exception for what a plugin in `addopts` loaded."""
    assert _decide(tmp_path, monkeypatch, MOD, {"sub.mod": OTHER_MOD},
                   before={"sub.mod"}) == ""


@pytest.mark.parametrize("file, named", [
    (OTHER_MOD, OTHER_MOD),
    (NOT_AT_LAYOUT, None),
], ids=["at-layout", "not-at-layout"])
def test_the_name_loaded_before_the_plugin_is_named_only_at_its_layout(
        tmp_path, monkeypatch, file, named):
    """What the harness had loaded under the target's name is let be, but
    for the target's own layout outside the copy, which is what a plugin in
    `addopts` that imports the target loaded."""
    assert _decide(tmp_path, monkeypatch, MOD, {"pkg.sub.mod": file},
                   before={"pkg.sub.mod"}) == (
        _file(tmp_path, named) if named else "")


def test_the_top_of_the_copy_is_never_part_of_the_name(tmp_path, monkeypatch):
    """The run starts at the top of the copy, so that directory is on
    `sys.path` and nothing above it is, an `__init__.py` there or not."""
    assert _decide(tmp_path, monkeypatch, "pkg/sub/mod.py",
                   {"pkg.sub.mod": OTHER_MOD},
                   copy={"__init__.py": "", "pkg/__init__.py": "",
                         "pkg/sub/__init__.py": "", "pkg/sub/mod.py": ""}) \
        == _file(tmp_path, OTHER_MOD)


def test_a_package_s_init_is_the_package(tmp_path, monkeypatch):
    assert _decide(tmp_path, monkeypatch, "src/pkg/__init__.py",
                   {"pkg": "other/src/pkg/__init__.py"}) \
        == _file(tmp_path, "other/src/pkg/__init__.py")


#: A regular package below a namespace package: `src/acme` has no
#: `__init__.py`, so the target is named `logging.handlers` from the copy,
#: which is the standard library's name, while the tests import it as
#: `acme.logging.handlers`.
UNDER_A_NAMESPACE = {"src/acme/logging/__init__.py": "",
                     "src/acme/logging/handlers.py": "def f():\n    return 1\n"}
HANDLERS = "src/acme/logging/handlers.py"


def test_a_target_whose_name_is_the_library_s_is_cleared_by_its_own_file(
        tmp_path, monkeypatch):
    """The tests loaded the copy's file under its full name, and the
    library's `logging.handlers` besides."""
    assert _decide(tmp_path, monkeypatch, HANDLERS, {
        "acme.logging.handlers": "copy/" + HANDLERS,
        "logging.handlers": logging.handlers.__file__},
        copy=UNDER_A_NAMESPACE) == ""


@pytest.mark.parametrize("target, copy, loaded", [
    (HANDLERS, UNDER_A_NAMESPACE,
     {"logging.handlers": logging.handlers.__file__}),
    ("src/acme/logging/__init__.py", UNDER_A_NAMESPACE,
     {"logging": logging.__file__}),
    ("tools/queue.py", {"tools/queue.py": ""}, {"queue": queue.__file__}),
], ids=["module", "package", "loose"])
def test_the_interpreter_s_own_library_is_never_the_one_named(
        tmp_path, monkeypatch, target, copy, loaded):
    """The tests import the library's module the copy's file is named like,
    and nothing says they loaded the copy's: the library is never where a
    project's file is, so it is not named. The loose file's tests run it in
    a child interpreter."""
    assert _decide(tmp_path, monkeypatch, target, loaded, copy=copy) == ""


@pytest.mark.parametrize("file, named", [
    ("lib/python3.X/pkg/sub/mod.py", None),
    ("lib/python3.X/site-packages/pkg/sub/mod.py", True),
    ("lib/python3.X/dist-packages/pkg/sub/mod.py", True),
], ids=["library", "site-packages", "dist-packages"])
def test_a_project_installed_inside_the_library_s_directory_is_named(
        tmp_path, monkeypatch, file, named):
    """Outside a virtual environment the site directory lies inside the
    library's, and a project installed there is a file to name."""
    assert _decide(tmp_path, monkeypatch, MOD, {"pkg.sub.mod": file},
                   library="lib/python3.X") == (
        _file(tmp_path, file) if named else "")


@pytest.mark.parametrize("loaded, named", [
    ({"acme.logging.handlers": "copy/" + HANDLERS,
      "logging.handlers": "other/lib/logging/handlers.py"}, None),
    ({"logging.handlers": "other/lib/logging/handlers.py"}, True),
], ids=["copy-s-own-loaded", "not-loaded"])
def test_a_name_another_package_holds_is_cleared_by_the_copy_s_own_file(
        tmp_path, monkeypatch, loaded, named):
    """A third party's `logging.handlers` holds the name the copy gives the
    target. The copy's own file loaded under any name is the mutant the
    tests saw; without it, the other file is named."""
    assert _decide(tmp_path, monkeypatch, HANDLERS, loaded,
                   copy=UNDER_A_NAMESPACE) == (
        _file(tmp_path, "other/lib/logging/handlers.py") if named else "")


@pytest.mark.parametrize("file, named", [
    ("other/lib/packaging/__init__.py", None),
    ("other/lib/packaging.py", True),
], ids=["a-package", "a-module-file"])
def test_a_loose_target_is_not_the_package_the_tests_import_under_its_name(
        tmp_path, monkeypatch, file, named):
    """`tools/packaging.py` and the third-party package `packaging` share a
    name, but a package's `__init__.py` is no file the loose module could
    be, so the tests' import of it says nothing of the target; another
    module file under the name is the target loaded from elsewhere."""
    assert _decide(tmp_path, monkeypatch, "tools/packaging.py",
                   {"packaging": file}, copy={"tools/packaging.py": ""}) == (
        _file(tmp_path, file) if named else "")


def test_a_package_below_a_namespace_named_like_the_library_s_runs(tmp_path):
    """The tests import the library's `logging.handlers` and the copy's
    `acme.logging.handlers`, and the copy's `src` comes first on the path,
    so the mutant is the file they ran against and is killed."""
    project = os.path.realpath(tmp_path / "proj")
    write_tree(project, {
        "pyproject.toml": ("[project]\nname = 'acme'\nversion = '0'\n"
                           "[tool.pytest.ini_options]\n"),
        "src/acme/logging/__init__.py": "",
        "src/acme/logging/handlers.py": ("def handle(x):\n"
                                         "    if x < 0:\n"
                                         "        raise ValueError(x)\n"
                                         "    return x\n"),
        "tests/conftest.py": (
            "import os\n"
            "import sys\n"
            "\n"
            "sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname("
            "os.path.abspath(__file__))), 'src'))\n"),
        "tests/test_handlers.py": ("import logging.handlers\n"
                                   "\n"
                                   "import pytest\n"
                                   "\n"
                                   "from acme.logging import handlers\n"
                                   "\n"
                                   "def test_negative():\n"
                                   "    with pytest.raises(ValueError):\n"
                                   "        handlers.handle(-1)\n")})

    done = pytest_in(project, "--mutate", HANDLERS, "--mutate-only", "RAISE",
                     "tests/test_handlers.py")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "1/1 killed" in done.stdout


def test_a_run_that_breaks_the_project_s_rules_fails_as_a_test_would(repo):
    write_tree(repo, {"pyproject.toml":
                      "[tool.pytest.ini_options]\n"
                      "[tool.invective]\nfail-on-survivors = true\n"})

    done = pytest_in(repo, "--mutate", "pkg/gate.py", "--mutate-only", "BOOL",
                     "pkg/tests/test_gate.py")

    assert done.returncode == 1, done.stdout + done.stderr
    assert ("fails:     %s: 1 survivor(s) that no comment accepts"
            % _p("pkg/gate.py")) in done.stdout


def test_a_ref_that_lacks_a_selected_test_is_refused(repo):
    """The selection is collected from the files as they stand; at a ref, a
    test added since is not there to run."""
    with open(os.path.join(repo, "pkg", "tests", "test_gate.py"),
              encoding="utf-8") as fh:
        source = fh.read()
    write_tree(repo, {"pkg/tests/test_gate.py":
                      source + "\ndef test_added_since():\n    pass\n"})

    done = pytest_in(repo, "--mutate", "pkg/gate.py", "--mutate-ref", "HEAD",
                     "pkg/tests/test_gate.py")

    assert done.returncode == 2
    assert "1 of the tests selected are not in the tree" in done.stdout
    assert "test_added_since" in done.stdout


def test_a_settings_file_the_tree_lacks_is_refused_for_that(repo):
    """An uncommitted `pytest.ini`, which this pytest read and the ref does
    not hold: every run would die loading it, and a refusal that called that
    a red baseline would send a person to fix tests that pass."""
    write_tree(repo, {"pkg/tests/pytest.ini": "[pytest]\nxfail_strict = true\n"})

    done = pytest_in(repo, "--mutate=pkg/gate.py", "--mutate-only", "RAISE",
                     "--mutate-ref", "HEAD", "pkg/tests")

    assert done.returncode == 2, done.stdout + done.stderr
    assert _p("pkg/tests/pytest.ini") in done.stdout
    assert "is RED on the unmutated tree" not in done.stdout


@pytest.mark.parametrize("options, killed", [
    ([], 0),
    (["-W", "error::UserWarning"], 1),
    (["-o", "filterwarnings=error::UserWarning"], 1),
])
def test_options_that_change_how_tests_run_reach_every_mutant_s_run(
        repo, tmp_path, options, killed):
    """Only a warning turned into an error can tell `<` from `<=` here."""
    commit(repo, {
        "pkg/warn.py": ("import warnings\n"
                        "\n"
                        "def f(n):\n"
                        "    if n < 0:\n"
                        "        warnings.warn('negative', UserWarning)\n"
                        "    return n\n"),
        "pkg/tests/test_warn.py": ("from pkg import warn\n"
                                   "\n"
                                   "def test_zero():\n"
                                   "    assert warn.f(0) == 0\n"),
    })
    out = tmp_path / "reports.json"

    done = pytest_in(repo, *options, "--mutate", "pkg/warn.py",
                     "--mutate-only", "CMP", "--mutate-json", str(out),
                     "pkg/tests/test_warn.py")

    assert done.returncode == 0, done.stdout + done.stderr
    (report,) = json.loads(out.read_text(encoding="utf-8"))
    assert report["killed"] == killed


def _plugin_here():
    """This checkout's plugin, loaded afresh: the one pytest loaded for this
    run comes from wherever invective is installed, which in a run of
    invective on itself is not the copy that holds the mutant."""
    spec = importlib.util.spec_from_file_location(
        "pytest_invective", os.path.join(SRC, "pytest_invective",
                                         "__init__.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(inipath=None):
    """A run's config as `_forwarded` reads it, read from *inipath*."""
    option = SimpleNamespace(
        plugins=["myplugin", "no:terminal", "no:xdist", "pytest_invective"],
        override_ini=["xfail_strict=true"], importmode="importlib")
    given = {"pythonwarnings": ["error"], "runxfail": True,
             "strict_markers": False}
    return SimpleNamespace(
        option=option, inipath=inipath,
        getoption=lambda name, default=None: given.get(name, default))


def test_the_options_forwarded_are_those_that_change_how_tests_run(tmp_path):
    """Every `-p` the run was given, but for the plugins whose options each
    mutant's run is given anyway, and a flag only when it was set."""
    assert _plugin_here()._forwarded(_config(), str(tmp_path)) == (
        "-p", "myplugin", "-o", "xfail_strict=true", "-W", "error",
        "--import-mode=importlib", "--runxfail")


def test_the_settings_file_is_forwarded_from_the_top(tmp_path):
    """Given from the root, where every run starts."""
    root = tmp_path / "proj"
    ini = root / "tests" / "pytest.ini"

    assert _plugin_here()._forwarded(_config(ini), str(root))[-3:] == (
        "--runxfail", "-c", os.path.join("tests", "pytest.ini"))


def test_a_settings_file_outside_the_root_is_not_forwarded(tmp_path):
    """No path from the copy leads to it; `pytest_file_left_out` decides
    whether the run may go ahead without it."""
    root = tmp_path / "proj"
    ini = tmp_path / "pyproject.toml"

    assert "-c" not in _plugin_here()._forwarded(_config(ini), str(root))
