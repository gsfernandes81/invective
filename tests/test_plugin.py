"""`pytest --mutate`: the plugin, driven by pytest itself in a real process."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import commit, write_tree

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
    outside = tmp_path / "outside" / "test_outside.py"
    outside.parent.mkdir()
    outside.write_text("def test_outside():\n    pass\n", encoding="utf-8")

    done = pytest_in(repo, "--mutate", "pkg/gate.py", str(outside))

    assert done.returncode == 2
    assert "is outside the project" in done.stdout
    assert "copy:" not in done.stdout


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
    assert "invective" not in done.stdout.replace("plugins: invective", "")


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
        "killer": "test_two.py::test_first", "missing": []}


def test_a_run_that_breaks_the_project_s_rules_fails_as_a_test_would(repo):
    write_tree(repo, {"pyproject.toml":
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


def test_the_options_forwarded_are_those_that_change_how_tests_run():
    """Every `-p` the run was given, but for the plugins whose options each
    mutant's run is given anyway, and a flag only when it was set."""
    option = SimpleNamespace(
        plugins=["myplugin", "no:terminal", "no:xdist", "pytest_invective"],
        override_ini=["xfail_strict=true"], importmode="importlib")
    given = {"pythonwarnings": ["error"], "runxfail": True,
             "strict_markers": False}
    config = SimpleNamespace(
        option=option, getoption=lambda name, default=None: given.get(name,
                                                                      default))

    assert _plugin_here()._forwarded(config) == (
        "-p", "myplugin", "-o", "xfail_strict=true", "-W", "error",
        "--import-mode=importlib", "--runxfail")
