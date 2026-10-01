"""`pytest --mutate`: the plugin, driven by pytest itself in a real process."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from conftest import commit, write_tree

#: This checkout's own `src`, ahead of any installed copy, so that the pytest
#: started here loads the plugin under test.
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")

MINOR = "pkg/tests/test_gate.py::test_a_minor_is_refused"
ADULT = "pkg/tests/test_gate.py::test_an_adult_is_admitted"


def _p(rel):
    return os.path.join(*rel.split("/"))


def pytest_in(repo, *args):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
         "--color=no", *args],
        cwd=repo, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONPATH": SRC})


def test_each_module_is_measured_against_the_collected_tests(repo, tmp_path):
    """Two modules, one selection, a report for each.

    With no configuration file, pytest's root is the tests' own directory, so
    its node ids start at `test_gate.py`; each run starts at the top of the
    worktree, so the ids it is handed have to start there.
    """
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
    assert "worktree:" not in done.stdout


def test_a_red_baseline_is_refused(repo):
    commit(repo, {"pkg/tests/test_red.py": "def test_red():\n    assert False\n"})

    done = pytest_in(repo, "--mutate", "pkg/gate.py", "pkg/tests/test_red.py")

    assert done.returncode == 2
    assert "is RED on the unmutated tree" in done.stdout


def test_a_collection_error_stops_the_run_before_any_mutant(repo):
    """pytest's own loop stops a run that could not collect; so does this one."""
    write_tree(repo, {"pkg/tests/test_broken.py": "def broken(:\n"})

    done = pytest_in(repo, "--mutate", "pkg/gate.py", "pkg/tests")

    assert done.returncode == 2
    assert "error during collection" in done.stdout
    assert "worktree:" not in done.stdout


def test_collect_only_lists_the_tests_and_mutates_nothing(repo):
    done = pytest_in(repo, "--collect-only", "--mutate", "pkg/gate.py",
                     "pkg/tests/test_gate.py")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "test_a_minor_is_refused" in done.stdout
    assert "worktree:" not in done.stdout


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
    """Under pytest-xdist the controlling process collects nothing.

    pytest-xdist is not installed here; its option is, by a root conftest,
    under xdist's long name (pytest keeps lowercase short options for
    itself), and that option is all the plugin reads.
    """
    write_tree(repo, {
        "pytest.ini": "[pytest]\n",
        "conftest.py": ("def pytest_addoption(parser):\n"
                        "    parser.addoption('--numprocesses', dest='numprocesses')\n"),
    })

    done = pytest_in(repo, "--numprocesses", "2", "--mutate", "pkg/gate.py",
                     "pkg/tests/test_gate.py")

    assert done.returncode == 4
    assert "-n 0" in done.stderr
