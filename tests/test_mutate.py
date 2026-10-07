"""The engine, and the ways its report could say something that is not so."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import time

import pytest
from pytest import ExitCode

from invective import mutate
from invective import tree as trees
from invective.tree import git_ref, working_tree

from conftest import FILES, commit, git, write_tree

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


@pytest.mark.parametrize("source, kind, change, mutant", [
    ("x = a < b", "CMP", "Lt -> LtE", "x = a <= b"),
    ("x = a is not b", "CMP", "IsNot -> Is", "x = a is b"),
    ("x = a and b", "BOOL", "And -> Or", "x = a or b"),
    ("x = not a", "NOT", "not X -> X", "x = a"),
    ("x = True", "CONST", "True -> False", "x = False"),
    ("x = False", "CONST", "False -> True", "x = True"),
    ("x = 3", "CONST", "3 -> 4", "x = 4"),
    ("raise E", "RAISE", "raise ... -> pass", "pass"),
])
def test_a_mutant_is_the_change_its_description_names(source, kind, change,
                                                      mutant):
    """The description is what a person reads; the mutant is what ran.

    Each source holds exactly one site, so a site that went missing, or one
    that turned into another kind, shows up as well as a wrong rewrite.
    """
    tree = ast.parse(source)

    assert [(k, w) for k, _n, w in mutate._sites(tree)] == [(kind, change)]
    assert ast.unparse(mutate._apply(tree, 0)) == mutant
    # And written into the source, the mutant is that text, bare, inside the
    # site's span: the one place the exact spelling of every kind is pinned.
    assert mutate._text_of(source, tree, 0) == (mutant, True)


def test_a_nested_bool_op_is_wrapped_so_it_stays_nested():
    """`a or b or c` is one flat `or`, a different tree from `(a or b) or c`
    with the same meaning; the bare text is wrong here, and the splice must
    notice and write the parenthesised form rather than fall back."""
    tree = ast.parse("x = a and b or c\n")

    # The outer `or` is site 0, the inner `and` site 1.
    assert [w for _k, _n, w in mutate._sites(tree)] == ["Or -> And", "And -> Or"]
    assert mutate._text_of("x = a and b or c\n", tree, 1) == (
        "x = (a or b) or c\n", True)


#: Formatting `ast.unparse` loses: a comment on a mutated line, odd spacing,
#: a `raise` and a compare each across two lines, a compare continued with a
#: backslash (where the bare text padded to two lines is a syntax error and
#: only the parenthesised form holds), and a trailing comment.
FORMATTED = (
    "def check(a, b, flag):  # kept\n"
    "    if a <  b:   # odd spacing, and a comment on the mutated line\n"
    "        raise ValueError(\n"
    "            'too small')\n"
    "    if (a <=\n"
    "            b and flag):\n"
    "        return True\n"
    "    if a <= \\\n"
    "            b:\n"
    "        return 1\n"
    "    return not flag  # trailing comment\n")


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_a_mutant_differs_from_its_source_only_inside_the_node_s_span(ending):
    """Issue #1's done-when: outside the mutated node's span the mutant is
    the source, byte for byte, every line keeps its number, and what the
    span holds parses to the mutant `_apply` makes. Under each of the three
    endings the tokenizer counts as a line break, since positions are the
    tokenizer's and a lone `\\r` is a line to it too.
    """
    source = FORMATTED.replace("\n", ending)
    tree = ast.parse(source)
    before = mutate._LINE_END.split(source)[::2]
    sites = mutate._sites(tree)
    assert len(sites) == 8, [k for k, _n, _w in sites]

    for index, (kind, node, what) in enumerate(sites):
        text, spliced = mutate._text_of(source, tree, index)
        assert spliced, (kind, what)
        after = mutate._LINE_END.split(text)[::2]
        assert len(after) == len(before), (kind, what)
        changed = [n + 1 for n, (x, y) in enumerate(zip(before, after)) if x != y]
        assert set(changed) <= set(range(node.lineno, node.end_lineno + 1)), (
            kind, what, changed)
        first = before[node.lineno - 1].encode()[:node.col_offset]
        last = before[node.end_lineno - 1].encode()[node.end_col_offset:]
        assert after[node.lineno - 1].encode().startswith(first)
        assert after[node.end_lineno - 1].encode().endswith(last)
        assert ast.dump(ast.parse(text)) == ast.dump(mutate._apply(tree, index))


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

    def red(where, tests, timeout, *rest):
        runs.append(where)
        return mutate.Verdict(False, ExitCode.TESTS_FAILED, "1 failed, 0 passed",
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

    def collects_nothing(where, tests, timeout, *rest):
        return mutate.Verdict(False, ExitCode.NO_TESTS_COLLECTED,
                              "no tests ran", "")

    monkeypatch.setattr(mutate, "run_tests", collects_nothing)
    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(*args)
    assert "collected nothing" in str(caught.value)

    # And again, with a baseline that really is green, so the refusal can only
    # be coming from the mutant's own run.
    seen = []

    def green_then_nothing(where, tests, timeout, *rest):
        seen.append(1)
        if len(seen) == 1:
            return mutate.Verdict(True, 0, "1 passed", "")
        return mutate.Verdict(False, ExitCode.NO_TESTS_COLLECTED,
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

    def green_then_killed(where, tests, timeout, *rest):
        calls.append(1)
        if len(calls) == 1:
            return mutate.Verdict(True, 0, "1 passed", "")
        return mutate.Verdict(False, ExitCode.TESTS_FAILED, "1 failed",
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
        assert kill["code"] == ExitCode.TESTS_FAILED
    # A test failing is not the same kill as a module that would not import.
    assert report["broken"] == 0


def test_the_mutant_run_asks_for_the_failure_lines_and_its_own_plugin(
        monkeypatch):
    """`-q` twice is silence, and the plugin is where the killer comes from.

    The run's working directory is the worktree, so it reads the target
    repository's pytest configuration, whose `addopts` may already carry `-q`.
    A second one on the command line STACKS into `-qq`, and pytest then prints
    no summary line and no node id at all, which leaves a person nothing to
    read in the tail of a refused run. `-rf` is the other half: under plain
    `-q` the short summary is suppressed too.

    Asserted on the argv rather than by running pytest: the claim is about
    what this function ASKS FOR.
    """
    seen = {}

    class Spy:
        returncode = 0

        def __init__(self, argv, **kwargs):
            seen["argv"], seen["env"] = argv, kwargs["env"]

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(mutate.subprocess, "Popen", Spy)
    got = mutate.run_tests("/nowhere", ["t.py"], timeout=1)
    argv = seen["argv"]
    assert argv.count("-q") == 0, argv
    assert "-rf" in argv, argv
    assert argv[argv.index("pytest_invective") - 1] == "-p", argv
    assert seen["env"]["INVECTIVE_VERDICT"].endswith("verdict.json")
    # The stub wrote no verdict, so the run names no killer.
    assert got.killer == ""


def test_the_killer_is_the_whole_id_whatever_pytest_prints_around_it(
        tmp_path, monkeypatch):
    """Read from the plugin, a node id cannot be cut by what surrounds it.

    Printed, the id below is coloured (pytest colours a pipe when asked, here
    by `PY_COLORS`), it holds spaces, and it holds a ` - ` of its own, which is
    the separator pytest prints between an id and its message.
    """
    (tmp_path / "test_ports.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.parametrize('pair', ['2380 and 2381 - two ports'])\n"
        "def test_two_ports_are_refused(pair):\n"
        "    assert False, 'the two disagree'\n", encoding="utf-8")
    monkeypatch.setenv("PY_COLORS", "1")

    got = mutate.run_tests(str(tmp_path), ["test_ports.py"], timeout=60)

    assert got.code == ExitCode.TESTS_FAILED
    assert got.killer == ("test_ports.py::test_two_ports_are_refused"
                          "[2380 and 2381 - two ports]")
    assert "\x1b" not in got.tail, "the tail is read by a person"


def test_a_run_that_starts_pytest_again_keeps_its_own_verdict(tmp_path):
    """The variable is the run's alone: a pytest the suite starts does not
    see it, so cannot write a verdict over this one's.
    """
    (tmp_path / "test_env.py").write_text(
        "import os\n"
        "\n"
        "def test_the_verdict_file_is_not_handed_on():\n"
        "    assert 'INVECTIVE_VERDICT' not in os.environ\n", encoding="utf-8")

    got = mutate.run_tests(str(tmp_path), ["test_env.py"], timeout=60)

    assert got.ok and got.killer == ""


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

    assert report["target"] == GATE and report["tests"] == GATE_TESTS
    assert report["mutants"] == 2 and report["killed"] == 1
    assert report["broken"] == 0
    (kill,) = report["kills"]
    assert kill["kind"] == "RAISE" and kill["line"] == 3
    assert kill["code"] == ExitCode.TESTS_FAILED
    assert kill["killer"] == "pkg/tests/test_gate.py::test_a_minor_is_refused"
    (survivor,) = report["survivors"]
    assert survivor["kind"] == "BOOL" and survivor["line"] == 4
    assert survivor["source"] == "if member and age >= 65:"


def test_every_mutant_s_entry_carries_its_diff(repo):
    """The kill and the survivor of the real run each say what the edit was,
    as a unified diff of the file against the mutant: one line out, one in,
    since the mutant is the file with one span edited."""
    report = mutate.mutate(repo, os.path.join(repo, GATE), GATE_TESTS,
                           ["RAISE", "BOOL"], None)

    (kill,) = report["kills"]
    (survivor,) = report["survivors"]
    for entry, out, into in (
            (kill, "        raise ValueError('under age')", "        pass"),
            (survivor, "    if member and age >= 65:",
             "    if member or age >= 65:")):
        head, body = (entry["diff"].split("\n")[:2],
                      entry["diff"].split("\n")[3:])
        assert head == ["--- pkg/gate.py", "+++ pkg/gate.py (mutant)"]
        assert [x for x in body if x[:1] == "-"] == ["-" + out], entry
        assert [x for x in body if x[:1] == "+"] == ["+" + into], entry


def test_a_run_leaves_the_project_as_it_was_and_its_copy_gone(tree, capsys):
    """Every mutant is written in the copy; none reaches the project, and the
    copy is removed at the end."""
    def snapshot():
        found = {}
        for dirpath, _dirs, files in os.walk(tree):
            for name in files:
                full = os.path.join(dirpath, name)
                with open(full, "rb") as fh:
                    found[os.path.relpath(full, tree)] = fh.read()
        return found

    before = snapshot()
    mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS, None, None)

    assert snapshot() == before
    where = capsys.readouterr().out.split("copy:", 1)[1].split()[0]
    assert "invective-" in where and not os.path.exists(where)


@pytest.mark.parametrize("ending", ["\r\n", "\r"])
def test_a_mutant_of_a_crlf_or_cr_file_differs_from_the_file_only_inside_the_span(
        tree, monkeypatch, ending):
    """The file as written, through the engine: a Windows checkout's `\\r\\n`
    endings are what the mutant is built from, so it differs from the file
    at the mutation and not at every line, and a lone-`\\r` file is read by
    the report line by line as the parser counts them.
    """
    write_tree(tree, {"pkg/gate.py": FILES["pkg/gate.py"].replace("\n", ending)})
    gate = os.path.join(tree, GATE)
    with open(gate, "rb") as fh:
        original = fh.read()
    reads = []

    def run(where, tests, timeout, *rest):
        with open(os.path.join(where, GATE), "rb") as fh:
            reads.append(fh.read())
        return mutate.Verdict(True, 0, "", "")

    monkeypatch.setattr(mutate, "run_tests", run)
    report = mutate.mutate(tree, gate, GATE_TESTS, ["RAISE", "CMP"], None)

    # The baseline's read is the file; each mutant's differs on its line only.
    assert reads[0] == original and len(reads) == 4
    lines = re.split(rb"\r\n|\r|\n", original)
    for mutant, entry in zip(reads[1:], report["survivors"]):
        got = re.split(rb"\r\n|\r|\n", mutant)
        assert len(got) == len(lines)
        assert [n + 1 for n, (x, y) in enumerate(zip(lines, got)) if x != y] == [
            entry["line"]], entry
        assert mutant.count(ending.encode()) == original.count(ending.encode())
    assert [s["source"] for s in report["survivors"]] == [
        "if age < 18:", "raise ValueError('under age')",
        "if member and age >= 65:"]


def test_a_site_the_splice_cannot_hold_falls_back_to_the_whole_file_and_says_so(
        tree, monkeypatch):
    """`pass` padded to the two lines of the `raise` leaves the `; y = 3` on
    a line of its own, which is no statement, so this mutant is the whole
    file unparsed, as every mutant once was -- flagged in its entry, since
    its line numbers are not the file's, and counted in the closing lines.
    """
    source = "def f():\n    raise E(\n        1); y = 3\n"
    tree_ = ast.parse(source)
    (index,) = [i for i, (k, _n, _w) in enumerate(mutate._sites(tree_))
                if k == "RAISE"]
    assert mutate._text_of(source, tree_, index) == (
        ast.unparse(mutate._apply(tree_, index)), False)

    write_tree(tree, {"pkg/gate.py": source})
    monkeypatch.setattr(mutate, "run_tests",
                        lambda *a, **k: mutate.Verdict(True, 0, "", ""))
    report = mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS, None, None)

    # The `raise` alone; the two constants beside it are spliced and unflagged.
    assert [(s["kind"], s.get("whole_file")) for s in report["survivors"]] == [
        ("RAISE", True), ("CONST", None), ("CONST", None)]
    assert mutate.summary(report)[-1] == (
        "           1 of the mutants could not be written inside their node's "
        "span and were written as a reformatted file")


def test_a_test_that_reads_the_module_s_own_source_survives_a_mutant_elsewhere(
        tree):
    """Issue #1's other done-when, with nothing stubbed: a test that reads the
    module's source through `inspect.getsource` is not failed by a mutant of
    another function, which it would be if the mutant's formatting were
    `ast.unparse`'s and the comment it looks for were gone.
    """
    write_tree(tree, {
        "pkg/own.py": ("def f():\n"
                       "    return 1  # kept\n"
                       "\n"
                       "\n"
                       "def g(x):\n"
                       "    if x < 0:\n"
                       "        raise ValueError\n"
                       "    return x\n"),
        "pkg/tests/test_own.py": (
            "import inspect\n"
            "from pkg import own\n"
            "\n"
            "def test_the_comment_is_kept():\n"
            "    assert '# kept' in inspect.getsource(own.f)\n"),
    })

    report = mutate.mutate(tree, os.path.join(tree, "pkg", "own.py"),
                           ["pkg/tests/test_own.py"], ["RAISE"], None)

    assert report["mutants"] == 1 and report["killed"] == 0
    assert [s["line"] for s in report["survivors"]] == [7]


@pytest.mark.parametrize("selection, code", [
    (["pkg/tests/test_gate.py", "-k", "no_test_has_this_name"],
     ExitCode.NO_TESTS_COLLECTED),
    (["pkg/tests/test_gate.py::test_that_is_not_there"], ExitCode.USAGE_ERROR),
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
    """`main` end to end, its project the current directory."""
    out = str(tmp_path / "report.json")

    assert mutate.main(["--target", GATE, "--tests", *GATE_TESTS,
                        "--only", "raise", "--json", out]) == 0

    assert "1/1 killed (100.0%), 0 survived" in capsys.readouterr().out
    with open(out, encoding="utf-8") as fh:
        report = json.load(fh)
    assert report["target"] == GATE and report["killed"] == 1


def test_a_run_needs_no_git_and_no_repository(tree, monkeypatch, capsys):
    """Without `--ref`, nothing asks git anything: the tree here is no
    repository, and a call to git would fail the test."""
    monkeypatch.chdir(tree)
    real = mutate.subprocess.run

    def run(argv, **kwargs):
        assert argv[0] != "git", argv
        return real(argv, **kwargs)

    monkeypatch.setattr(mutate.subprocess, "run", run)

    assert mutate.main(["--target", GATE, "--tests", *GATE_TESTS,
                        "--only", "RAISE"]) == 0
    assert "1/1 killed (100.0%)" in capsys.readouterr().out


def test_an_operator_that_does_not_exist_is_refused_by_name(repo, capsys):
    """A mistyped `--only` would otherwise select no sites and say so vaguely."""
    assert mutate.main(["--target", GATE, "--tests", *GATE_TESTS,
                        "--only", "CMP,RISE"]) == 2
    assert "RISE" in capsys.readouterr().err


def test_the_module_is_mutated_as_it_stands_not_as_committed(repo):
    """Uncommitted edits are in the run: the line given for the refusal's
    mutant is where the edited file has it, one below where it was committed.
    """
    gate = os.path.join(repo, GATE)
    with open(gate, encoding="utf-8") as fh:
        edited = "# an uncommitted line\n" + fh.read()
    write_tree(repo, {"pkg/gate.py": edited})

    report = mutate.mutate(repo, gate, GATE_TESTS, ["RAISE"], None)

    assert report["killed"] == report["mutants"] == 1
    assert report["kills"][0]["line"] == 4
    with open(gate, encoding="utf-8") as fh:
        assert fh.read() == edited, "the project is not the run's to touch"


def test_a_module_left_out_of_the_copy_is_refused(tree, monkeypatch):
    monkeypatch.setattr(mutate, "run_tests",
                        lambda *a, **k: pytest.fail("ran the selection"))

    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS, None, None,
                      tree=working_tree(tree, ("pkg/gate.py",)))
    assert "not in the tree the mutants are made in" in str(caught.value)


def test_a_module_outside_the_project_is_refused(tree, tmp_path,
                                                 monkeypatch):
    """Joined to the copy, `../x.py` names a file that is not in it."""
    outside = tmp_path / "elsewhere.py"
    outside.write_text("def f():\n    raise ValueError\n", encoding="utf-8")
    monkeypatch.setattr(mutate, "working_tree",
                        lambda *a: pytest.fail("made a copy"))

    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(tree, str(outside), GATE_TESTS, None, None)
    assert "outside the project" in str(caught.value)


def test_a_ref_in_a_repository_with_no_commit_is_refused_with_git_s_reason(
        tree):
    """There is no HEAD to make a worktree of, and git's words say so."""
    git(tree, "init", "-q", "-b", "main")

    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS, None, None,
                      tree=git_ref(tree, "HEAD"))
    assert str(caught.value).startswith("git worktree failed: fatal: ")
    assert "HEAD" in str(caught.value)


def test_a_ref_is_mutated_as_committed_not_as_it_stands(repo):
    """The other way round from a run on the files as they stand."""
    gate = os.path.join(repo, GATE)
    with open(gate, encoding="utf-8") as fh:
        edited = "# an uncommitted line\n" + fh.read()
    write_tree(repo, {"pkg/gate.py": edited})

    report = mutate.mutate(repo, gate, GATE_TESTS, ["RAISE"], None,
                           tree=git_ref(repo, "HEAD"))

    assert report["kills"][0]["line"] == 3
    assert len(git(repo, "worktree", "list").splitlines()) == 1


def test_a_ref_from_below_the_top_of_the_repository_is_that_directory(repo):
    """A project can be a directory inside a repository; the copy of the ref
    stands for that directory, not for the repository's top."""
    with git_ref(os.path.join(repo, "pkg"), "HEAD") as where:
        assert sorted(os.listdir(where))[:2] == ["__init__.py", "gate.py"]


def test_every_write_of_the_module_gets_a_second_of_its_own(repo, monkeypatch):
    """The stale-bytecode verdict again, this time across the whole loop.

    CPython keys a cached `.pyc` on the source's mtime in whole seconds and
    its size, so every write must land in a later second than the one before
    it, and the first in a later second than the worktree's own copy.
    """
    seen = []
    real = mutate._write

    def write(path, text, when):
        if not seen:
            seen.append(int(os.stat(path).st_mtime))
        seen.append(when)
        real(path, text, when)

    monkeypatch.setattr(mutate, "_write", write)
    monkeypatch.setattr(mutate, "run_tests",
                        lambda *a, **k: mutate.Verdict(True, 0, "", ""))

    report = mutate.mutate(repo, os.path.join(repo, GATE), GATE_TESTS, None,
                           None)

    assert len(seen) == report["mutants"] + 1 == 7
    assert all(a < b for a, b in zip(seen, seen[1:])), seen


def test_progress_is_said_after_every_tenth_mutant(repo, monkeypatch, capsys):
    commit(repo, {"pkg/many.py": "X = [%s]\n" % ", ".join(
        str(n) for n in range(12))})

    def run(where, tests, timeout, *rest):
        print("ran")
        return mutate.Verdict(True, 0, "", "")

    monkeypatch.setattr(mutate, "run_tests", run)
    mutate.mutate(repo, os.path.join(repo, "pkg", "many.py"), GATE_TESTS,
                  ["CONST"], None)

    said = [ln.strip() for ln in capsys.readouterr().out.splitlines()
            if ln.strip() == "ran" or ln.startswith("  ...")]
    # the baseline, ten mutants, the line, then the last two
    assert said == ["ran"] * 11 + ["... 10/12, 10 survived"] + ["ran"] * 2


@pytest.mark.parametrize("selection, code", [
    (["pkg/tests/test_ready.py"], ExitCode.INTERRUPTED),
    (["pkg/tests/test_ready.py::test_ready"], ExitCode.USAGE_ERROR),
])
def test_a_mutant_that_breaks_the_import_is_a_blunter_kill(repo, selection,
                                                           code):
    """Killed, because the suite noticed; and counted apart, because what
    noticed was a module that would not import and not a test failing.

    Named as a file, pytest calls that a collection error. Named by node id,
    as the plugin names its selection, pytest finds no collectors and exits
    as it does for a usage error, the code that otherwise means the harness
    broke; the failed module it reports is what tells the two apart.
    """
    commit(repo, {
        "pkg/ready.py": ("READY = True\n"
                         "if not READY:\n"
                         "    raise RuntimeError('not ready')\n"),
        "pkg/tests/test_ready.py": ("from pkg import ready\n"
                                    "\n"
                                    "def test_ready():\n"
                                    "    assert ready.READY\n"),
    })

    report = mutate.mutate(repo, os.path.join(repo, "pkg", "ready.py"),
                           selection, ["CONST", "NOT"], None)

    assert report["killed"] == report["broken"] == 2
    assert {k["code"] for k in report["kills"]} == {code}
    # A module that will not collect is named as the killer.
    assert {k["killer"] for k in report["kills"]} == {"pkg/tests/test_ready.py"}


def test_workers_in_the_repository_s_own_options_are_turned_off(repo):
    """`-n` in addopts reaches every mutant's run, and each is run with
    `-n 0`, which wins over it; a run with xdist turned off altogether would
    not know the option, and every run would be refused.
    """
    pytest.importorskip("xdist")
    commit(repo, {"pytest.ini": "[pytest]\naddopts = -n 2\n"})

    report = mutate.mutate(repo, os.path.join(repo, GATE), GATE_TESTS,
                           ["RAISE"], None)

    assert [k["killer"] for k in report["kills"]] == [
        "pkg/tests/test_gate.py::test_a_minor_is_refused"]


def test_a_run_that_hangs_is_stopped_with_everything_it_started(tmp_path):
    """Counted as killed; and stopped whole, because a suite can start
    processes of its own, and killing the run alone leaves them running.

    The test below starts a process that writes a heartbeat, waits for the
    first beat, and hangs. Once the run is stopped, the heartbeat must stop.
    """
    beat = tmp_path / "beat"
    (tmp_path / "test_hang.py").write_text(
        "import os, subprocess, sys, time\n"
        "\n"
        "BEAT = %r\n"
        "\n"
        "def test_hang():\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time\\n'\n"
        "                      'while True:\\n'\n"
        "                      '    open(%%r, \"a\").write(\".\")\\n'\n"
        "                      '    time.sleep(0.05)' %% BEAT])\n"
        "    while not os.path.exists(BEAT):\n"
        "        time.sleep(0.05)\n"
        "    time.sleep(60)\n" % str(beat), encoding="utf-8")

    got = mutate.run_tests(str(tmp_path), ["test_hang.py"], timeout=3)

    assert got == mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT")
    assert beat.exists(), "the test never got as far as starting the process"
    before = beat.stat().st_size
    time.sleep(0.5)
    assert beat.stat().st_size == before, "a process the run started lives on"


@pytest.mark.parametrize("argv, missing", [
    (["--tests", "t.py"], "--target"),
    (["--target", "m.py"], "--tests"),
])
def test_the_command_says_which_argument_is_missing(capsys, argv, missing):
    with pytest.raises(SystemExit) as stopped:
        mutate.main(argv)
    assert stopped.value.code == 2
    assert missing in capsys.readouterr().err


def test_an_interrupted_run_is_stopped_and_the_interrupt_goes_on(monkeypatch):
    """The run is in a process group of its own, so the ^C that interrupts
    invective never reaches it: invective has to stop it, and then stop."""
    stopped = []

    class Interrupted:
        def __init__(self, argv, **kwargs):
            pass

        def communicate(self, timeout=None):
            raise KeyboardInterrupt

    monkeypatch.setattr(mutate.subprocess, "Popen", Interrupted)
    monkeypatch.setattr(mutate, "_stop", stopped.append)

    with pytest.raises(KeyboardInterrupt):
        mutate.run_tests("/nowhere", ["t.py"], timeout=1)
    assert len(stopped) == 1


def test_a_baseline_that_runs_out_of_time_is_refused_for_that(repo, monkeypatch):
    """Not as a red baseline: nothing failed, and a person told it did would
    go looking for a failing test."""
    monkeypatch.setattr(mutate, "run_tests", lambda *a, **k: mutate.Verdict(
        False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT"))

    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(repo, os.path.join(repo, GATE), GATE_TESTS, ["RAISE"],
                      None)
    assert "took longer than %d s" % mutate.BASELINE_TIMEOUT in str(caught.value)
    assert "RED" not in str(caught.value)


def test_kills_by_time_are_said_apart_from_the_rest():
    """A kill by time is the one a slow machine can give a mutant the suite
    would have let through, so the closing lines count it on its own."""
    report = {"killed": 3, "mutants": 4, "survivors": [{}], "broken": 1,
              "accepted": [], "stale": [],
              "kills": [{"code": mutate.TIMED_OUT}, {"code": mutate.TIMED_OUT},
                        {"code": ExitCode.INTERRUPTED}]}

    assert mutate.summary(report) == [
        "3/4 killed (75.0%), 1 survived",
        "           2 of the kills were runs stopped at their time budget, "
        "not a test failing",
        "           1 of the kills were collection or internal errors, "
        "not a test failing"]


def test_a_cleanup_that_fails_does_not_hide_git_s_reason(tree, monkeypatch):
    """When git cannot make the worktree, its words are the refusal; a
    directory that will not be removed must not replace them."""
    git(tree, "init", "-q", "-b", "main")
    real = trees.shutil.rmtree

    def rmtree(path, ignore_errors=False):
        if not ignore_errors:
            raise PermissionError(path)
        real(path, ignore_errors=True)

    monkeypatch.setattr(trees.shutil, "rmtree", rmtree)
    with pytest.raises(mutate.Refusal) as caught:
        with git_ref(tree, "HEAD"):
            pass
    assert str(caught.value).startswith("git worktree failed: fatal: ")


def test_a_ref_s_worktree_git_would_not_remove_is_not_left_listed(repo,
                                                                 monkeypatch):
    """On Windows a file the stopped run held open is enough to make
    `git worktree remove` fail; the repository must not keep listing it."""
    real = mutate.subprocess.run

    def run(argv, **kwargs):
        if argv[:1] == ["git"] and "remove" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "locked")
        return real(argv, **kwargs)

    monkeypatch.setattr(mutate.subprocess, "run", run)
    monkeypatch.setattr(mutate, "run_tests",
                        lambda *a, **k: mutate.Verdict(True, 0, "", ""))
    mutate.mutate(repo, os.path.join(repo, GATE), GATE_TESTS, ["RAISE"], None,
                  tree=git_ref(repo, "HEAD"))

    assert len(git(repo, "worktree", "list").splitlines()) == 1


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="a POSIX session")
def test_a_process_that_left_the_run_s_group_does_not_hold_invective(
        tmp_path, monkeypatch):
    """A process that starts a session of its own escapes the kill, and if
    it holds the run's output open, waiting for that output never ends.

    It holds it only when pytest is not capturing output, which is what the
    `-s` below is for; under capture it inherits a file of pytest's instead.
    """
    monkeypatch.setattr(mutate, "_STOP_GRACE", 0.5)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -s\n",
                                         encoding="utf-8")
    (tmp_path / "test_escape.py").write_text(
        "import os, subprocess, sys, time\n"
        "\n"
        "def test_escape():\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; "
        "time.sleep(30)'], start_new_session=True)\n"
        "    time.sleep(60)\n", encoding="utf-8")

    started = time.monotonic()
    got = mutate.run_tests(str(tmp_path), ["test_escape.py"], timeout=2)

    assert got.code == mutate.TIMED_OUT
    assert time.monotonic() - started < 15, "waited on the escaped process"


ACCEPTING = (
    "def admit(age, member):\n"
    "    if age < 18:  # invective: accept[equivalent: 18 -> 19] a year apart\n"
    "        raise ValueError('under age')  # invective: accept[equivalent] why\n"
    "    # invective: accept[untestable] nothing here checks seniors\n"
    "    if member and age >= 65:\n"
    "        return 'senior'\n"
    "    x = 1  # invective: accept[equivalent: 1 -> 3] no such mutant\n"
    "    # invective: accept[equivalent] above a blank line\n"
    "\n"
    "    return 'adult'\n")


def test_each_acceptance_is_judged_against_what_the_mutants_did(tree,
                                                                monkeypatch):
    """A stand-in run kills what the real tests would: the refusal's removal
    and the boundary moved to 18, and nothing else."""
    write_tree(tree, {"pkg/gate.py": ACCEPTING})

    def run(where, tests, timeout, *rest):
        with open(os.path.join(where, GATE), encoding="utf-8") as fh:
            text = fh.read()
        killed = "raise ValueError" not in text or "age <= 18" in text
        return mutate.Verdict(not killed, 1 if killed else 0, "",
                              "pkg/tests/test_gate.py::t" if killed else "")

    monkeypatch.setattr(mutate, "run_tests", run)
    report = mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS, None,
                           None)

    # Accepted by name, and the boundary beside it killed as it should be.
    by_line = {(a["line"], a["change"]) for a in report["accepted"]}
    assert (2, "18 -> 19") in by_line and (2, "Lt -> LtE") not in by_line
    assert by_line == {(2, "18 -> 19"), (5, "And -> Or"), (5, "GtE -> Gt"),
                       (5, "65 -> 66")}
    assert {a["reason"] for a in report["accepted"] if a["line"] == 5} == {
        "untestable"}
    assert all("diff" in a for a in report["accepted"])
    assert [{k: v for k, v in e.items() if k != "diff"}
            for e in report["survivors"]] == [
        {"kind": "CONST", "line": 7, "change": "1 -> 2", "source": "x = 1  "
         "# invective: accept[equivalent: 1 -> 3] no such mutant"}]
    assert [(s["line"], s["problem"]) for s in report["stale"]] == [
        (3, "raise ... -> pass is killed by pkg/tests/test_gate.py::t"),
        (7, "line 7 has no mutant 1 -> 3"),
        (8, "line 9 has no mutant at all")]
    assert mutate.summary(report)[1:3] == [
        "           4 of the survivors are accepted in the source",
        "           3 acceptance(s) are stale: what they claim is not so"]


def test_the_command_fails_a_run_that_breaks_the_project_s_rules(tree,
                                                                 monkeypatch,
                                                                 capsys):
    """With `fail-on-survivors`, a survivor no comment accepts fails the run
    with 1, and accepting it in the source passes it."""
    monkeypatch.chdir(tree)
    write_tree(tree, {"pyproject.toml":
                      "[tool.invective]\nfail-on-survivors = true\n"})
    argv = ["--target", GATE, "--tests", *GATE_TESTS, "--only", "BOOL"]

    assert mutate.main(argv) == 1
    assert "fails:     1 survivor(s) that no comment accepts" in (
        capsys.readouterr().out)

    with open(os.path.join(tree, GATE), encoding="utf-8") as fh:
        source = fh.read()
    write_tree(tree, {"pkg/gate.py": source.replace(
        "    if member and", "    # invective: accept[untestable] no senior "
        "test\n    if member and")})
    assert mutate.main(argv) == 0


def test_a_ref_given_to_the_command_is_the_tree_it_runs_on(repo, capsys):
    gate = os.path.join(repo, GATE)
    with open(gate, encoding="utf-8") as fh:
        source = fh.read()
    write_tree(repo, {"pkg/gate.py": "# an uncommitted line\n" + source})
    out = os.path.join(repo, "..", "report.json")

    assert mutate.main(["--target", GATE, "--tests", *GATE_TESTS, "--only",
                        "RAISE", "--ref", "HEAD", "--json", out]) == 0
    with open(out, encoding="utf-8") as fh:
        assert json.load(fh)["kills"][0]["line"] == 3


def test_a_selected_test_the_tree_does_not_have_is_refused(tree):
    """A selection collected somewhere else: one of its tests is not here."""
    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(tree, os.path.join(tree, GATE), [], ["RAISE"], None,
                      selection=["pkg/tests/test_gate.py::test_a_minor_is_refused",
                                 "pkg/tests/test_gate.py::test_written_since"])
    assert str(caught.value) == (
        "1 of the tests selected are not in the tree the mutants are made in, "
        "among them pkg/tests/test_gate.py::test_written_since")


def test_a_refused_command_exits_2_and_says_why(tree, monkeypatch, capsys):
    monkeypatch.chdir(tree)

    assert mutate.main(["--target", "pkg/nowhere.py", "--tests",
                        *GATE_TESTS]) == 2
    assert "refused: pkg/nowhere.py is not in the tree" in (
        capsys.readouterr().err.replace(os.sep, "/"))


@pytest.mark.parametrize("tests", [
    ["tests/test_gate.py"],
    ["tests/test_gate.py::test_a_minor_is_refused"],
])
def test_the_command_runs_from_a_subdirectory_of_the_project(tree, monkeypatch,
                                                             capsys, tests):
    """From `pkg` the project is still the whole tree: copied from its top,
    the target and the tests typed from `pkg` found where they are. Copied
    from `pkg` alone, the copy holds no `pkg` to import."""
    write_tree(tree, {"pyproject.toml": ""})
    monkeypatch.chdir(os.path.join(tree, "pkg"))

    assert mutate.main(["--target", "gate.py", "--tests", *tests,
                        "--only", "RAISE"]) == 0
    said = capsys.readouterr().out
    assert "project:   %s" % tree in said
    assert "target:    %s" % GATE in said
    assert "1/1 killed (100.0%)" in said


def test_the_value_after_an_expression_or_plugin_option_is_left_as_typed(
        tree):
    """`tests` is an entry of `pkg`, and still no path after `-k`, `-m` or
    `-p`, nor in `-m=tests`, which has one dash and is no `--option=`; after
    `--option=` it is one. A path out of the project is given whole, and
    what names nothing is passed as typed."""
    pkg = os.path.join(tree, "pkg")
    test_gate = os.path.join("pkg", "tests", "test_gate.py")

    assert mutate.rewrite_tests(
        ["tests/test_gate.py", "-k", "tests", "-m", "tests", "-p", "tests",
         "-m=tests", "--deselect=tests/test_gate.py::x", "--rootdir=../..",
         "-q", "tests/test_none.py::x"], pkg, tree) == [
        test_gate, "-k", "tests", "-m", "tests", "-p", "tests", "-m=tests",
        "--deselect=%s::x" % test_gate, "--rootdir=%s" % os.path.dirname(tree),
        "-q", "tests/test_none.py::x"]
