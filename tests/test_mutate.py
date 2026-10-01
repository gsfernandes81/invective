"""The engine, and the ways its report could say something that is not so."""

from __future__ import annotations

import ast
import json
import os

import pytest

from invective import mutate

from conftest import git

SAMPLE = '''
def refuse(n, flag, other):
    """A docstring, which must never be mutated."""
    if n <= 3 and flag:
        raise ValueError("too small")
    if not other:
        return True
    return False
'''

GATE = os.path.join("pkg", "gate.py")
GATE_TESTS = ["pkg/tests/test_gate.py"]


def _kinds(source):
    return [k for k, _n, _w in mutate._sites(ast.parse(source))]


def test_every_operator_in_the_vocabulary_carries_its_reason():
    """An operator with no sentence beside it is one nobody can judge.

    The survivors are read by a person deciding whether a check is missing or
    the line does not matter, and the operator's own description is half of
    what they have to go on.
    """
    for name, why in mutate.OPERATORS.items():
        assert why.strip(), name
        assert len(why) > 20, "%s: a label, not a reason" % name


def test_the_sample_offers_every_kind_of_site():
    found = set(_kinds(SAMPLE))
    assert {"CMP", "BOOL", "NOT", "CONST", "RAISE"} <= found, found


def test_a_docstring_is_never_a_mutation_site():
    """A mutated message is noise: either nothing pins it, or everything does.

    Numbers and booleans decide things; strings mostly get printed. Mutating
    them produces a run where most survivors are messages and the real
    survivors are lost in them.
    """
    tree = ast.parse(SAMPLE)
    for kind, node, _what in mutate._sites(tree):
        if kind == "CONST":
            assert not isinstance(node.value, str), ast.dump(node)


def test_the_reported_line_is_the_line_that_was_broken():
    """`_apply` must break the site `_sites` named, or the report lies.

    The two walk separate trees -- the original for reporting, a deep copy for
    mutating -- and they correspond only because `ast.walk` is deterministic.
    If that ever stops being true the report still looks perfectly plausible
    and points at the wrong lines, which is worse than no report.
    """
    tree = ast.parse(SAMPLE)
    sites = mutate._sites(tree)
    idx = next(i for i, (k, _n, _w) in enumerate(sites) if k == "RAISE")
    mutated = ast.unparse(mutate._apply(tree, idx))
    assert "raise ValueError" not in mutated
    # and the original is untouched: the caller reuses it for every mutant
    assert "raise ValueError" in ast.unparse(tree)


def test_one_mutant_changes_exactly_one_thing():
    tree = ast.parse(SAMPLE)
    base = ast.unparse(tree)
    seen = set()
    for i in range(len(mutate._sites(tree))):
        out = ast.unparse(mutate._apply(tree, i))
        assert out != base, "site %d changed nothing" % i
        seen.add(out)
    assert len(seen) == len(mutate._sites(tree)), "two sites collided"


def test_the_raise_operator_really_removes_the_refusal():
    """The operator class that catches a deleted refusal, driven end to end."""
    tree = ast.parse(SAMPLE)
    idx = next(i for i, (k, _n, _w) in enumerate(mutate._sites(tree))
               if k == "RAISE")
    ns = {}
    exec(compile(mutate._apply(tree, idx), "<mutant>", "exec"), ns)
    # The refusal is gone, so the call that should have raised falls through.
    assert ns["refuse"](1, True, False) is True


def test_a_red_baseline_is_a_refusal_and_not_a_perfect_score(repo, monkeypatch):
    """If the selection is already failing, every mutant is "killed" by a
    failure that has nothing to do with the mutation and the report reads
    100%. So the engine refuses instead.

    **The red comes from a stub, and the selection below really passes.**
    What is claimed here is what `mutate` does with a red verdict, so the
    refusal can only be coming from the verdict `run_tests` returned, not
    from a selection that was bogus to begin with.

    **One call, not none and not many.** A refusal fires BEFORE the first
    mutant; a perfect score is what a run that went on to "kill" every one of
    them would report, and counting the runs is what tells those two apart.
    """
    runs = []

    def red(where, tests, timeout):
        runs.append(where)
        return mutate.Verdict(False, mutate.TESTS_FAILED, "1 failed, 0 passed",
                              "pkg/tests/test_x.py::test_y")

    monkeypatch.setattr(mutate, "run_tests", red)
    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(repo, os.path.join(repo, GATE), GATE_TESTS, ["RAISE"], 1)
    assert "RED" in str(caught.value)
    assert len(runs) == 1, runs


def test_a_run_that_collected_nothing_is_never_a_kill(repo, monkeypatch):
    """A selection that collects NOTHING makes pytest exit 5, and reading
    "non-zero" as "the suite noticed" kills every mutant with a run that
    executed no test at all. The score is identical to a perfectly guarded
    module.

    Both ends are checked because they fail for different reasons. At the
    baseline it means the selection was wrong. Mid-run it cannot mean that --
    the baseline just proved this selection collects -- so it means the runner
    broke, and charging that to the mutant is the lie.
    """
    args = (repo, os.path.join(repo, GATE), GATE_TESTS, ["RAISE"], 2)

    def collects_nothing(where, tests, timeout):
        return mutate.Verdict(False, mutate.NO_TESTS_COLLECTED,
                              "no tests ran", "")

    monkeypatch.setattr(mutate, "run_tests", collects_nothing)
    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(*args)
    assert "collected nothing" in str(caught.value)

    # And again, with a baseline that really is green, so the refusal can only
    # be coming from the mutant's own run.
    seen = []

    def green_then_nothing(where, tests, timeout):
        seen.append(1)
        if len(seen) == 1:
            return mutate.Verdict(True, 0, "1 passed", "")
        return mutate.Verdict(False, mutate.NO_TESTS_COLLECTED,
                              "no tests ran", "")

    monkeypatch.setattr(mutate, "run_tests", green_then_nothing)
    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(*args)
    assert "collected nothing" in str(caught.value)
    assert "runner and not the mutation" in str(caught.value)


def test_the_report_says_which_test_killed_each_mutant(repo, monkeypatch):
    """A score says the suite noticed; a killer says what is load-bearing.

    A line killed only by a test that is nominally about something else is a
    finding, and one that cannot be seen at all when the report counts kills
    without naming them.
    """
    calls = []

    def green_then_killed(where, tests, timeout):
        calls.append(1)
        if len(calls) == 1:
            return mutate.Verdict(True, 0, "1 passed", "")
        return mutate.Verdict(False, mutate.TESTS_FAILED, "1 failed",
                              "pkg/tests/test_gate.py::test_%d" % len(calls))

    monkeypatch.setattr(mutate, "run_tests", green_then_killed)
    report = mutate.mutate(repo, os.path.join(repo, GATE), GATE_TESTS,
                           ["CMP"], 3)

    assert report["killed"] == len(report["kills"]) == 2
    assert not report["survivors"]
    # Every kill names a test, and they are the ids the stub returned rather
    # than anything this test invented.
    assert [k["killer"] for k in report["kills"]] == [
        "pkg/tests/test_gate.py::test_2", "pkg/tests/test_gate.py::test_3"]
    for kill in report["kills"]:
        assert kill["code"] == mutate.TESTS_FAILED
    # A test failing is not the same kill as a module that would not import.
    assert report["broken"] == 0


def test_the_mutant_run_asks_for_the_failure_lines_and_does_not_double_quiet(
        monkeypatch):
    """`-q` twice is silence, and silence is what the killer has to be read from.

    The run's working directory is the worktree, so it reads the target
    repository's pytest configuration, whose `addopts` may already carry `-q`.
    A second one on the command line STACKS into `-qq`, and pytest then prints
    no summary line and no node id at all. `-rf` is the other half: under
    plain `-q` the short summary is suppressed too, and the `FAILED` lines
    have to be asked for by name.

    Asserted on the argv rather than by running pytest: the claim is about
    what this function ASKS FOR.
    """
    seen = {}

    class Done:
        returncode = 0
        stdout = ""

    def spy(argv, **kwargs):
        seen["argv"] = argv
        return Done()

    monkeypatch.setattr(mutate.subprocess, "run", spy)
    mutate.run_tests("/nowhere", ["t.py"], timeout=1)
    argv = seen["argv"]
    assert argv.count("-q") == 0, argv
    assert "-rf" in argv, argv


def test_the_killer_is_read_through_pytest_s_colour(monkeypatch):
    """`^FAILED` does not match a line that begins with an escape sequence.

    pytest can emit SGR colour on this output even when its stdout is a pipe
    rather than a terminal, so the summary line arrives as
    `\\x1b[31mFAILED\\x1b[0m ...` and the node id is broken up by escapes in
    the middle (`::\\x1b[1mtest_name\\x1b[0m`). Matched raw, the killer comes
    back empty and every kill is anonymous; matched loosely, it comes back
    with escapes embedded and the id is unusable as an id.

    The sample below is pytest's own bytes, not an approximation of them.
    """
    coloured = (
        "\x1b[36m\x1b[1m=========== short test summary info ============\x1b[0m\n"
        "\x1b[31mFAILED\x1b[0m tests/test_x.py::"
        "\x1b[1mtest_this_one_fails\x1b[0m - assert 1 == 2\n"
        "\x1b[31m================ \x1b[31m\x1b[1m1 failed\x1b[0m\x1b[31m "
        "in 0.05s\x1b[0m\x1b[31m ================\x1b[0m\n")

    class Done:
        returncode = 1
        stdout = coloured

    monkeypatch.setattr(mutate.subprocess, "run", lambda *a, **k: Done())
    got = mutate.run_tests("/nowhere", ["t.py"], timeout=1)
    assert got.killer == "tests/test_x.py::test_this_one_fails"
    assert "\x1b" not in got.tail, "the tail is read by a person too"


def test_a_parametrised_killer_survives_the_space_in_its_own_id(monkeypatch):
    """A node id can contain spaces.

    `FAILED <nodeid> - <message>` is separated by ` - `, but a parametrised id
    carries the parameter's repr between its brackets -- so the id itself holds
    spaces and taking it as `\\S+` cuts it at the first one. What comes back
    then is `...refused[2380`: not something any `-k` or node-id lookup can
    find, and indistinguishable from a whole id in the report.

    **The parameter below contains a ` - ` of its own**, which is the case that
    rules out the obvious repair. Splitting the line on the separator looks
    right until a parameter carries one, and then the cut lands inside the
    brackets instead. The id is bounded by its own `]`, so that is what the
    pattern anchors on.
    """
    class Done:
        returncode = 1
        stdout = ("FAILED tests/test_ports.py::test_two_ports_are_refused"
                  "[2380 and 2381 - two ports] - Refusal: the two disagree\n")

    monkeypatch.setattr(mutate.subprocess, "run", lambda *a, **k: Done())
    got = mutate.run_tests("/nowhere", ["t.py"], timeout=1)
    assert got.killer == ("tests/test_ports.py::test_two_ports_are_refused"
                          "[2380 and 2381 - two ports]")


def test_two_mutants_of_a_line_cannot_share_a_cached_pyc(tmp_path):
    """The stale-bytecode verdict, which reads exactly like a real one.

    CPython keys its `.pyc` on the source's `(mtime, size)` and stores that
    mtime as WHOLE SECONDS. `<` for `>` is the same number of bytes, and two
    mutant writes land inside one second whenever the selection is quick -- so
    the second mutant is judged on the first one's bytecode, and nothing in
    the output says so. What is asserted is the pair the interpreter actually
    compares, not the wall clock.
    """
    path = tmp_path / "m.py"
    stamps = set()
    for n in range(3):
        mutate._write(str(path), "x = %d < 1\n" % n, 1_700_000_000 + 2 * n)
        got = os.stat(path)
        assert got.st_size == 10, "the sizes must collide for this to matter"
        stamps.add(int(got.st_mtime))
    assert len(stamps) == 3, stamps


def test_a_real_run_names_the_killer_and_the_line_nothing_checks(repo):
    """The whole path with nothing stubbed: worktree, pytest, verdicts.

    `gate.py` refuses a minor and a test checks that, so removing the `raise`
    is killed and the report names that test. Nothing checks the `and` on the
    senior line, so turning it into `or` survives.
    """
    report = mutate.mutate(repo, os.path.join(repo, GATE), GATE_TESTS,
                           ["RAISE", "BOOL"], None)

    assert report["target"] == GATE
    assert report["mutants"] == 2 and report["killed"] == 1
    assert report["score"] == 50.0 and report["broken"] == 0
    (kill,) = report["kills"]
    assert kill["kind"] == "RAISE" and kill["line"] == 3
    assert kill["code"] == mutate.TESTS_FAILED
    assert kill["killer"] == "pkg/tests/test_gate.py::test_a_minor_is_refused"
    (survivor,) = report["survivors"]
    assert survivor["kind"] == "BOOL" and survivor["line"] == 4
    assert survivor["source"] == "if member and age >= 65:"


def test_a_run_leaves_the_checkout_and_its_worktrees_as_they_were(repo, capsys):
    """No mutant in the checkout, and no worktree left registered or on disk."""
    mutate.mutate(repo, os.path.join(repo, GATE), GATE_TESTS, ["RAISE"], None)

    assert git(repo, "status", "--porcelain") == ""
    assert len(git(repo, "worktree", "list").splitlines()) == 1
    where = capsys.readouterr().out.split("worktree:", 1)[1].split()[0]
    assert "invective-" in where and not os.path.exists(where)


@pytest.mark.parametrize("selection, code", [
    (["pkg/tests/test_gate.py", "-k", "no_test_has_this_name"],
     mutate.NO_TESTS_COLLECTED),
    (["pkg/tests/test_gate.py::test_that_is_not_there"], mutate.USAGE_ERROR),
])
def test_pytest_s_own_empty_selection_is_refused(repo, selection, code):
    """The same refusal as the stubbed one above, from pytest itself.

    The stub hands back this module's constants, so it would go on passing if
    a constant stopped being the code pytest really exits with.
    """
    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(repo, os.path.join(repo, GATE), selection, ["RAISE"], None)
    assert "collected nothing" in str(caught.value)
    assert "pytest exited %d" % code in str(caught.value)


def test_a_module_with_nothing_to_break_is_refused(repo):
    """An empty `__init__.py` has no score; it is not a module that scored 0."""
    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(repo, os.path.join(repo, "pkg", "__init__.py"),
                      GATE_TESTS, None, None)
    assert "no mutation sites" in str(caught.value)


def test_the_command_writes_the_report_it_printed(repo, tmp_path, capsys):
    """`main` end to end inside a repository, with the root asked of git."""
    out = str(tmp_path / "report.json")

    assert mutate.main(["--target", GATE, "--tests", *GATE_TESTS,
                        "--only", "raise", "--json", out]) == 0

    assert "1/1 killed (100.0%), 0 survived" in capsys.readouterr().out
    with open(out, encoding="utf-8") as fh:
        report = json.load(fh)
    assert report["target"] == GATE and report["killed"] == 1


def test_outside_a_repository_is_refused_before_anything_runs(
        tmp_path, monkeypatch, capsys):
    """Without a repository there is no HEAD to make a worktree of."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    ran = []
    monkeypatch.setattr(mutate, "run_tests", lambda *a, **k: ran.append(a))

    assert mutate.main(["--target", "m.py", "--tests", "t.py"]) == 2
    assert "not inside a git repository" in capsys.readouterr().err
    assert not ran


def test_an_operator_that_does_not_exist_is_refused_by_name(repo, capsys):
    """A mistyped `--only` would otherwise select no sites and say so vaguely."""
    assert mutate.main(["--target", GATE, "--tests", *GATE_TESTS,
                        "--only", "CMP,RISE"]) == 2
    assert "RISE" in capsys.readouterr().err
