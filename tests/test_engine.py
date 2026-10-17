"""The engine on the fixture projects, each run by a real pytest.

The tests hold the engine to the verdicts the projects' authors know, show that no
test runs in the parent and that each mutant is live in its child, and cover every
refusal of stage 1.
"""

from __future__ import annotations

import ast
import glob
import json
import os
import shutil

import pytest

from invective import environment

# The verdicts the docstrings of the modules under `tests/projects/basic/src/basic/`
# give; the mutants' ids are structural, so a reformat of the files leaves this table
# true.
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
    "basic.short:one:body:0": "killed",
    "basic.short:one:body:1": "killed",
    "basic.short:one:body:-1": "killed",
    "basic.short:Box.get:body:0": "killed",
    "basic.short:Box.get:body:1": "killed",
    "basic.short:Box.get:body:-1": "killed",
}


def test_the_one_line_functions_of_basic_keep_their_body_on_the_def_line():
    """Each function of `basic/short.py` has its whole body on its `def` line.

    Those functions are what the module exists for. A formatter splits each onto two
    lines, and every verdict in `BASIC` stays the same, so only this check notices.
    """
    path = os.path.join(
        os.path.dirname(__file__), "projects", "basic", "src", "basic", "short.py"
    )
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    assert [f.name for f in functions] == ["one", "get"]
    for function in functions:
        lines = {statement.lineno for statement in function.body}
        assert lines == {function.lineno}, function.name


def test_the_basic_projects_verdicts_are_the_ones_its_author_knows(project):
    """Every mutant of the basic project gets the verdict its docstring states."""
    run = project("basic")
    assert run.returncode == 0, run.out
    assert {mid: v for mid, (v, _k) in run.verdicts().items()} == BASIC


def test_a_kill_names_the_first_failing_test_and_its_phase(project):
    """A kill names the cheapest covering test that failed, and the phase it failed in."""
    run = project("basic")
    mutants = {m["id"]: m for m in run.report["mutants"]}
    # Both tests that cover `is_open` fail with `True`; the cheaper one runs first
    # and is the one named.
    assert mutants["basic.gate:is_open:body:True"]["tests"] == 2
    assert (
        mutants["basic.gate:is_open:body:True"]["killer"]
        == "tests/test_gate.py::test_open_hours"
    )
    assert mutants["basic.gate:is_open:body:True"]["when"] == "call"
    # The fixture's line is credited to both tests that use its result, not only
    # to the one that set it up.
    assert mutants["basic.gate:through_fixture:body:0"]["tests"] == 2


def test_the_parent_runs_no_test(project, tmp_path):
    """The tests wrote the pids of more than one child, and not the parent's."""
    pids = str(tmp_path / "pids")
    run = project("basic", env={"BASIC_PIDS": pids})
    assert run.returncode == 0, run.out
    with open(pids) as fh:
        seen = {int(line) for line in fh}
    assert run.pid not in seen
    assert len(seen) > 1


def test_the_console_report_lists_every_mutant_and_the_canary(project):
    """The console report has a line per mutant, the canary's killer and the score."""
    run = project("basic")
    assert "replace count -> int with 0  SURVIVED 2 tests" in run.out
    assert "replace value -> int with 0  SURVIVED 1 test" in run.out
    assert "canary killed by tests/test_gate.py::" in run.out
    assert (
        "31 mutants in 4 files: 19 killed, 8 survived, 2 unreached, 2 unmeasured; "
        "score 65.5%, 19 killed of 29"
    ) in run.out
    assert run.report["canary"].startswith("tests/test_gate.py::")


def test_the_baselines_run_shows_as_pytests_own(project):
    """The terminal shows the selection's run and its count, not `no tests ran`.

    The parent passes child 0's reports through its own hooks.
    """
    run = project("basic")
    assert "11 passed" in run.out
    assert "no tests ran" not in run.out


def test_a_red_baseline_is_refused(project):
    """A failing test in the baseline refuses the run, before any mutant.

    The refusal names the failing test and ends with the baseline's own output
    (uncaptured here, with `-s`). It names no file of the run's directory, which is
    gone by the time the user reads it.
    """
    run = project("red", "-s")
    assert run.returncode == 2, run.out
    refusal = run.out[run.out.index("refused the run") :]
    assert refusal.startswith(
        "refused the run: the selection fails before any mutant: "
        "tests/test_light.py::test_wrong"
    )
    assert "the light is red" in refusal
    assert "baseline.out" not in run.out
    assert run.report is None


def test_a_selection_that_runs_no_line_of_the_targets_is_refused(project):
    """A selection whose tests run no line of any target refuses the run."""
    run = project("untouched", "-k", "touches_nothing")
    assert run.returncode == 2, run.out
    assert "no test in the selection runs a line of the targets" in run.out


def test_a_canary_that_survives_is_refused(project):
    """A canary that no test fails refuses the run.

    The selected test runs a line of the module (a lambda's) but enters none of its
    functions, so it passes with the canary live, which is no evidence about any
    mutant.
    """
    run = project("untouched", "-k", "only_a_lambda")
    assert run.returncode == 2, run.out
    assert "the forced-fail canary survived" in run.out


def test_a_target_imported_before_instrumentation_is_refused(project):
    """A target a `-p` plugin imports before the finder exists refuses the run."""
    run = project("basic", "-p", "basic.gate", env={"PYTHONPATH": os.path.join("src")})
    assert run.returncode == 2, run.out
    assert "imported before invective could instrument it" in run.out
    assert "basic.gate" in run.out


def test_xdist_workers_are_refused(project):
    """Asking pytest-xdist for workers refuses the run."""
    run = project("basic", "-n", "2")
    assert run.returncode == 2, run.out
    assert "refused the run" in run.out
    assert "turn pytest-xdist's workers off with -n 0" in run.out


def test_a_missing_target_is_a_usage_error(project):
    """A `--mutate` path that does not exist is a usage error, exit status 4."""
    run = project("basic", targets=("src/basic/nowhere.py",))
    assert run.returncode == 4, run.out
    assert "no such file or directory" in run.out


def test_a_directory_target_takes_its_modules_but_not_its_tests(project):
    """A directory target takes the modules under it and leaves out its test modules."""
    run = project("basic", targets=(".",))
    assert run.returncode == 0, run.out
    assert run.report["targets"] == [
        "src/basic/__init__.py",
        "src/basic/gate.py",
        "src/basic/other.py",
        "src/basic/short.py",
    ]


def test_the_run_leaves_nothing_in_the_temporary_directory(project, tmp_path):
    """The run's directory is removed at the end, unless the user asks to keep it.

    A run with `environment.KEEP` set shows the directory is under the temporary
    directory; a run without it leaves nothing there.
    """
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    env = {"TMPDIR": str(tmp)}
    kept = project("basic", env={**env, environment.KEEP: "1"})
    assert kept.returncode == 0, kept.out
    assert len(glob.glob(str(tmp / "invective-*"))) == 1

    shutil.rmtree(tmp)
    tmp.mkdir()
    run = project("basic", env=env)
    assert run.returncode == 0, run.out
    assert glob.glob(str(tmp / "invective-*")) == []


def test_code_a_fixture_runs_is_credited_to_the_tests_that_use_it(project):
    """A mutant in code only a fixture runs is covered by the tests that use the fixture.

    An overridden fixture's set-up, and a fixture a test asks for in its body, both
    run code the test depends on: their mutants are killed, not unreached.
    """
    run = project("fixtures")
    assert run.returncode == 0, run.out
    override = "tests/test_override.py::test_the_override_sees_the_base"
    dynamic = "tests/test_dynamic.py::test_a_fixture_asked_for_in_the_body"
    assert run.verdicts() == {
        f"fixtures.calc:base:body:{value}": ("killed", override)
        for value in ("0", "1", "-1")
    } | {
        f"fixtures.calc:dynamic:body:{value}": ("killed", dynamic)
        for value in ("0", "1", "-1")
    }


def test_a_canary_kill_that_is_not_the_canarys_is_refused(project):
    """A canary child whose first failure is not `switch.Canary` refuses the run.

    The only test that runs the target fails in the canary child for a reason of its
    own (it depends on a test that does not run there), so the failure does not show
    that the selection runs the targets.
    """
    run = project("ordered", "-k", "fill or read")
    assert run.returncode == 2, run.out
    assert "the failure was not the canary's" in run.out
    assert "tests/test_order.py::test_read" in run.out
    assert "(call)" in run.out


def test_a_finalizer_that_raises_in_the_final_teardown_leaves_the_verdict(
    project, tmp_path
):
    """A finalizer that raises in a child's final teardown leaves the verdict written.

    A session fixture's finalizer calls the target in the teardown a child runs after
    its tests stop: with the canary it raises `Canary`, with a mutant `pytest.fail`.
    Neither is an `Exception`; each child still writes its verdict, and then an event
    naming what the teardown raised.
    """
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    run = project("finalizer", env={"TMPDIR": str(tmp), environment.KEEP: "1"})
    assert run.returncode == 0, run.out
    assert run.report["canary"] == "tests/test_shop.py::test_closing"
    assert run.verdicts() == {
        f"finalizer.shop:closing:body:{value}": (
            "killed",
            "tests/test_shop.py::test_closing",
        )
        for value in ("''", "'xyzzy'")
    }

    (rundir,) = tmp.glob("invective-*")
    raised = {}
    for events in rundir.glob("*.jsonl"):
        lines = [json.loads(line) for line in events.read_text().splitlines()]
        raised[events.name] = [
            line["type"] for line in lines if line.get("event") == "teardown-raised"
        ]
    assert raised == {
        "baseline.jsonl": [],
        "canary.jsonl": ["Canary"],
        "0.jsonl": ["Failed"],
        "1.jsonl": ["Failed"],
    }


def test_the_parent_imports_no_target(project):
    """The parent takes a target's mutants from its source, without importing it.

    The directory target holds a module that exits when imported and one that only
    a test imports. The first module's mutants are unreached; the second's function,
    which runs at its import inside the test, is measured in the child.
    """
    run = project("lazy")
    assert run.returncode == 0, run.out
    assert run.verdicts() == {
        "lazy.script:main:body:1": ("unreached", None),
        "lazy.script:main:body:-1": ("unreached", None),
        "lazy.late:compute:body:0": ("killed", "tests/test_late.py::test_late"),
        "lazy.late:compute:body:1": ("killed", "tests/test_late.py::test_late"),
        "lazy.late:compute:body:-1": ("killed", "tests/test_late.py::test_late"),
    }


@pytest.mark.parametrize(
    ("when", "baseline"), [("conftest", "no tests ran"), ("report", "1 passed")]
)
def test_threads_alive_in_the_parent_before_a_fork_are_refused(project, when, baseline):
    """A thread alive in the parent at a fork refuses the run.

    A thread a conftest starts at import is refused before child 0 is forked, so no
    test runs; one a hook starts when the parent replays the baseline's reports is
    refused before the canary is forked.
    """
    run = project("threads", env={"START_THREAD": when})
    assert run.returncode == 2, run.out
    assert (
        "threads are alive in the parent, so a forked child could deadlock: sleeper"
        in run.out
    )
    assert baseline in run.out


def test_the_map_holds_each_tests_wall_and_cpu_time_by_phase(project, tmp_path):
    """The map holds every test's wall and CPU seconds per phase, for order and budgets.

    Every basic test takes a fraction of a millisecond of CPU, which a clock with
    10 ms ticks reads as nothing.
    """
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    run = project("basic", env={"TMPDIR": str(tmp), environment.KEEP: "1"})
    assert run.returncode == 0, run.out
    (rundir,) = tmp.glob("invective-*")
    tests = json.loads((rundir / "map.json").read_text())["tests"]
    assert len(tests) == 11
    for nodeid, test in tests.items():
        assert set(test["wall"]) == set(test["cpu"]) == {"setup", "call", "teardown"}, (
            nodeid
        )
        assert all(seconds > 0 for seconds in test["wall"].values()), nodeid
        assert sum(test["cpu"].values()) > 0, nodeid


def test_a_child_that_ends_without_a_verdict_is_unmeasured(project):
    """A child that ends without a verdict leaves its mutant unmeasured.

    The note gives how the child ended and the end of its output.
    """
    run = project("early", "-s")
    assert run.returncode == 0, run.out
    mutants = {m["id"]: m for m in run.report["mutants"]}
    for value in ("0", "1", "-1"):
        mutant = mutants[f"early.exits:code:body:{value}"]
        assert mutant["verdict"] == "unmeasured", mutant
        assert mutant["note"].startswith("the child exited 5 without a verdict"), mutant
        assert "leaving without a verdict" in mutant["note"], mutant


def test_a_child_stops_at_the_first_failure(project, tmp_path):
    """A child runs no test after the first that fails.

    Each of `triple`'s mutants fails all three of its tests; the child that runs one
    reports on a single test, the one that killed it.
    """
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    run = project("early", env={"TMPDIR": str(tmp), environment.KEEP: "1"})
    assert run.returncode == 0, run.out
    triples = [m for m in run.report["mutants"] if m["function"] == "triple"]
    assert [(m["verdict"], m["tests"]) for m in triples] == [("killed", 3)] * 3

    (rundir,) = tmp.glob("invective-*")
    reported = {}
    for events in rundir.glob("[0-9]*.jsonl"):
        lines = [json.loads(line) for line in events.read_text().splitlines()]
        killer = next((line["killer"] for line in lines if "verdict" in line), None)
        if killer is not None and killer.startswith("tests/test_triple.py"):
            reported[events.name] = (
                killer,
                {line["nodeid"] for line in lines if line.get("event") == "report"},
            )
    assert len(reported) == 3
    assert all(nodeids == {killer} for killer, nodeids in reported.values()), reported


def test_an_empty_selection_is_refused(project):
    """A selection of no tests refuses the run."""
    run = project("basic", "-k", "nothing_matches")
    assert run.returncode == 2, run.out
    assert "refused the run: pytest collected no tests" in run.out


def test_a_collection_error_is_refused(project):
    """A collection error refuses the run: the selection is not the one asked for."""
    # Written at run time, so that no file in the tree fails to parse.
    run = project(
        "basic",
        "broken/test_syntax.py",
        files={"broken/test_syntax.py": "def test_broken(:\n    pass\n"},
    )
    assert run.returncode == 2, run.out
    assert "refused the run: 1 error(s) during collection" in run.out
