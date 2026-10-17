"""The engine on the fixture projects: the verdicts their authors know,
the proofs that the parent is pristine and the mutants are live, and
every refusal of stage 1."""

from __future__ import annotations

import os
import shutil

import pytest

# The verdicts `tests/projects/basic/src/basic/gate.py` and `other.py` say
# of themselves, in their docstrings; the mutants' ids are structural, so a
# reformat of the files leaves this table true.
BASIC = {
    "basic.gate:is_open:body:True": "killed",
    "basic.gate:is_open:body:False": "killed",
    "basic.gate:count:body:0": "survived",
    "basic.gate:count:body:1": "killed",
    "basic.gate:count:body:-1": "survived",
    "basic.gate:label:body:''": "unreached",
    "basic.gate:label:body:'xyzzy'": "unreached",
    "basic.gate:evens:body:iter([])": "survived",
    "basic.gate:evens:body:iter([0])": "survived",
    "basic.gate:evens:body:iter([1])": "killed",
    "basic.gate:evens:body:iter([-1])": "killed",
    "basic.gate:Counter.bump:body:None": "killed",
    "basic.gate:Counter.value:body:0": "survived",
    "basic.gate:Counter.value:body:1": "survived",
    "basic.gate:Counter.value:body:-1": "survived",
    "basic.gate:through_fixture:body:0": "killed",
    "basic.gate:through_fixture:body:1": "killed",
    "basic.gate:through_fixture:body:-1": "killed",
    "basic.gate:helper:body:''": "unmeasured",
    "basic.gate:helper:body:'xyzzy'": "unmeasured",
    "basic.other:twice:body:0": "killed",
    "basic.other:twice:body:1": "killed",
    "basic.other:twice:body:-1": "killed",
    "basic.other:sign:body:True": "survived",
    "basic.other:sign:body:False": "killed",
}


def test_the_basic_projects_verdicts_are_the_ones_its_author_knows(project):
    run = project("basic")
    assert run.returncode == 0, run.out
    assert {mid: v for mid, (v, _k) in run.verdicts().items()} == BASIC


def test_a_kill_names_the_first_failing_test_and_its_phase(project):
    run = project("basic")
    mutants = {m["id"]: m for m in run.report["mutants"]}
    assert mutants["basic.gate:is_open:body:True"]["killer"] == "tests/test_gate.py::test_open_hours"
    assert mutants["basic.gate:is_open:body:True"]["when"] == "call"
    # The fixture's line is credited to both tests that use its result, not
    # only to the one that happened to set it up.
    assert mutants["basic.gate:through_fixture:body:0"]["tests"] == 2


def test_the_parent_runs_no_test(project, tmp_path):
    """The parent's pid never appears among the pids the tests wrote, and
    more than one child did."""
    pids = str(tmp_path / "pids")
    run = project("basic", env={"BASIC_PIDS": pids})
    assert run.returncode == 0, run.out
    with open(pids) as fh:
        seen = {int(line) for line in fh}
    assert run.pid not in seen
    assert len(seen) > 1


def test_the_console_report_lists_every_mutant_and_the_canary(project):
    run = project("basic")
    assert "replace count -> int with 0  SURVIVED 2 tests" in run.out
    assert "replace value -> int with 0  SURVIVED 1 test" in run.out
    assert "canary killed by tests/test_gate.py::" in run.out
    assert "25 mutants in 3 files: 13 killed, 8 survived, 2 unreached, 2 unmeasured" in run.out
    assert run.report["canary"].startswith("tests/test_gate.py::")


def test_the_baselines_run_shows_as_pytests_own(project):
    """The parent replays child 0's reports, so the terminal shows the
    selection's run and its count, not `no tests ran`."""
    run = project("basic")
    assert "9 passed" in run.out
    assert "no tests ran" not in run.out


def test_a_red_baseline_is_refused(project):
    run = project("red")
    assert run.returncode == 2, run.out
    assert "refused the run: the selection fails before any mutant" in run.out
    assert "tests/test_light.py::test_wrong" in run.out
    assert run.report is None


def test_a_selection_that_runs_no_line_of_the_targets_is_refused(project):
    run = project("untouched", "-k", "touches_nothing")
    assert run.returncode == 2, run.out
    assert "no test in the selection runs a line of the targets" in run.out


def test_a_canary_that_survives_is_refused(project):
    """A test that runs a line of the module (a lambda's) but enters none
    of its functions passes with the canary live, which proves nothing
    about any mutant."""
    run = project("untouched", "-k", "only_a_lambda")
    assert run.returncode == 2, run.out
    assert "the forced-fail canary survived" in run.out


def test_a_target_imported_before_instrumentation_is_refused(project):
    """A plugin loaded with -p imports the target before the finder exists."""
    run = project("basic", "-p", "basic.gate",
                  env={"PYTHONPATH": os.path.join("src")})
    assert run.returncode == 2, run.out
    assert "imported before invective could instrument it" in run.out
    assert "basic.gate" in run.out


def test_xdist_workers_are_a_usage_error(project):
    run = project("basic", "-n", "2")
    assert run.returncode == 4, run.out
    assert "turn pytest-xdist's workers off with -n 0" in run.out


def test_a_missing_target_is_a_usage_error(project):
    run = project("basic", targets=("src/basic/nowhere.py",))
    assert run.returncode == 4, run.out
    assert "no such file or directory" in run.out


def test_a_directory_target_takes_its_modules_but_not_its_tests(project):
    run = project("basic", targets=(".",))
    assert run.returncode == 0, run.out
    assert run.report["targets"] == ["src/basic/__init__.py", "src/basic/gate.py",
                                     "src/basic/other.py"]


def test_the_run_leaves_nothing_in_the_temporary_directory(project, tmp_path):
    """The run's directory of child files is under the temporary directory,
    as a run told to keep it shows, and a run not told to removes it."""
    import glob
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    env = {"TMPDIR": str(tmp)}
    kept = project("basic", env={**env, "INVECTIVE_KEEP": "1"})
    assert kept.returncode == 0, kept.out
    assert len(glob.glob(str(tmp / "invective-*"))) == 1
    shutil.rmtree(tmp)
    tmp.mkdir()
    run = project("basic", env=env)
    assert run.returncode == 0, run.out
    assert glob.glob(str(tmp / "invective-*")) == []
