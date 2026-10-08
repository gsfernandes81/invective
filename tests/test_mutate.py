"""The engine, and the ways its report could say something that is not so."""

from __future__ import annotations

import ast
import copy
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import warnings

import pytest
from pytest import ExitCode

from invective import config, mutate, process
from invective import tree as trees
from invective.tree import git_ref

from conftest import (FILES, MARKERLESS, MONOREPO, WORKSPACE, commit, git,
                      monorepo, no_pytest_settings_above, slow_run,
                      write_tree)

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

#: This checkout's own `src`, for a run that is given a `PYTHONPATH`: the
#: plugin each run loads has to keep coming from here.
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")


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

    `_sites` names the node, and the module's path for it leads `_apply` to
    it. If the two ever stop meeting, the report still looks perfectly
    plausible and points at the wrong lines, which is worse than no report.
    """
    module = mutate._Module(SAMPLE)
    idx = next(i for i, (k, _n, _w) in enumerate(module.sites) if k == "RAISE")
    mutated = ast.unparse(mutate._apply(module, idx, module.tree))
    assert "raise ValueError" not in mutated
    # and the original is untouched: the caller reuses it for every mutant
    assert "raise ValueError" in ast.unparse(module.tree)


def test_one_mutant_changes_exactly_one_thing():
    module = mutate._Module(SAMPLE)
    base = ast.unparse(module.tree)
    seen = set()
    for i in range(len(module.sites)):
        out = ast.unparse(mutate._apply(module, i, module.tree))
        assert out != base, "site %d changed nothing" % i
        seen.add(out)
    assert len(seen) == len(module.sites), "two sites collided"


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
    module = mutate._Module(source)

    assert [(k, w) for k, _n, w in module.sites] == [(kind, change)]
    assert ast.unparse(mutate._apply(module, 0, module.tree)) == mutant
    # And written into the source, the mutant is that text, bare, inside the
    # site's span: the one place the exact spelling of every kind is pinned.
    assert mutate._text_of(module, 0) == (mutant, True)


def test_a_nested_bool_op_is_wrapped_so_it_stays_nested():
    """`a or b or c` is one flat `or`, a different tree from `(a or b) or c`
    with the same meaning; the bare text is wrong here, and the splice must
    notice and write the parenthesised form rather than fall back."""
    module = mutate._Module("x = a and b or c\n")

    # The outer `or` is site 0, the inner `and` site 1.
    assert [w for _k, _n, w in module.sites] == ["Or -> And", "And -> Or"]
    assert mutate._text_of(module, 1) == ("x = (a or b) or c\n", True)


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
    """Outside the mutated node's span the mutant is the source, byte for
    byte, every line keeps its number, and what the span holds parses to
    the mutant `_apply` makes. Under each of the three endings the
    tokenizer counts as a line break, since positions are the tokenizer's
    and a lone `\\r` is a line to it too.
    """
    source = FORMATTED.replace("\n", ending)
    module = mutate._Module(source)
    before = mutate._LINE_END.split(source)[::2]
    sites = module.sites
    assert len(sites) == 8, [k for k, _n, _w in sites]

    for index, (kind, node, what) in enumerate(sites):
        text, spliced = mutate._text_of(module, index)
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
        assert ast.dump(ast.parse(text)) == ast.dump(
            mutate._apply(module, index, module.tree))
        # A mutant is never its original.
        assert ast.dump(ast.parse(text)) != ast.dump(module.tree), (kind, what)


#: A t-string: a compare, an untouched `{x+1}` whose own constant is a site
#: too, a constant alone, a `not`, and a constant in a format spec.
TEMPLATE = ('def f(a, b, x):\n'
            '    return t"{a < b} {x+1} {1} {not a} {True!r:>{3}}"\n')


@pytest.mark.skipif(sys.version_info < (3, 14), reason="t-strings")
def test_a_site_inside_a_t_string_is_spliced_and_mutated():
    """3.14's interpolation carries its expression's source text, and the
    tree `_apply` makes keeps that text in step with the edit: without it
    the splice of `a <= b` reads as another program than the stale tree,
    and the whole file written instead is rebuilt from the stale text, so
    the mutant is the original program and survives every suite.
    """
    module = mutate._Module(TEMPLATE)
    texts = {}
    for index, (kind, _node, what) in enumerate(module.sites):
        text, spliced = mutate._text_of(module, index)
        assert spliced, (kind, what)
        assert ast.dump(ast.parse(text)) != ast.dump(module.tree), (kind, what)
        texts.setdefault((kind, what), []).append(text.split("\n")[1])
    line = '    return t"{a < b} {x+1} {1} {not a} {True!r:>{3}}"'
    assert texts == {
        ("CMP", "Lt -> LtE"): [line.replace("a < b", "a <= b")],
        ("CONST", "1 -> 2"): [line.replace("{1}", "{2}"),
                              line.replace("x+1", "x+2")],
        ("NOT", "not X -> X"): [line.replace("not a", "a")],
        ("CONST", "True -> False"): [line.replace("True", "False")],
        ("CONST", "3 -> 4"): [line.replace("{3}", "{4}")]}


@pytest.mark.skipif(sys.version_info < (3, 14), reason="t-strings")
@pytest.mark.parametrize("expression, mutated, kind", [
    ("a<b", "a <= b", "CMP"), ("not a", "a", "NOT")])
def test_a_t_string_site_the_splice_cannot_hold_is_written_mutated(
        expression, mutated, kind):
    """`{a<b = }` repeats the expression's text in the template's literal
    part, which the splice does not edit, so this mutant is the whole file
    unparsed -- and its interpolation is the edited expression, not the
    original one rebuilt from the text the parser gave it, while the
    untouched `{x+1}` beside it keeps its own text."""
    source = 'def f(a, b, x):\n    return t"{%s = } {x+1}"\n' % expression
    module = mutate._Module(source)
    (index,) = [i for i, (k, _n, _w) in enumerate(module.sites) if k == kind]
    text, spliced = mutate._text_of(module, index)
    assert not spliced
    assert [n.str for n in ast.walk(ast.parse(text))
            if isinstance(n, ast.Interpolation)] == [mutated, "x+1"]


@pytest.mark.skipif(sys.version_info < (3, 14), reason="t-strings")
def test_a_nested_t_string_site_written_whole_is_the_mutant():
    """An interpolation inside another's expression: the outer one's text is
    rebuilt from the inner one's, which must be the edited text already, or
    the file unparsed is the original program and survives every suite."""
    source = "def f(a, b):\n    return t\"{t'{a<b = }'}\"\n"
    module = mutate._Module(source)
    (index,) = [i for i, (k, _n, _w) in enumerate(module.sites) if k == "CMP"]
    text, spliced = mutate._text_of(module, index)
    assert not spliced
    assert [n.str for n in ast.walk(ast.parse(text))
            if isinstance(n, ast.Interpolation)] == ["t'a<b = {a <= b!r}'",
                                                     "a <= b"]
    assert ast.dump(ast.parse(text)) != ast.dump(module.tree)


@pytest.mark.skipif(sys.version_info < (3, 14), reason="t-strings")
def test_a_site_inside_a_nested_t_string_is_spliced():
    """The outer interpolation's text is written from the inner one's, so
    the comparison of the splice with the mutant normalises the inner first,
    as `_apply` does: from the inner's raw `x+2` the two would differ only
    in that spacing, and the mutant would be the whole file unparsed."""
    source = "def f(x):\n    return t\"{t'{x+1}'}\"\n"
    module = mutate._Module(source)
    (index,) = [i for i, (k, _n, _w) in enumerate(module.sites)
                if k == "CONST"]
    text, spliced = mutate._text_of(module, index)
    assert spliced is True
    assert text == source.replace("x+1", "x+2")


def test_every_site_below_a_non_ascii_line_is_spliced():
    """Positions count the UTF-8 bytes of a line, so the line starts do: a
    site below an em dash that is placed by characters is off by two bytes
    and cannot be held by the splice, which loses the file's formatting."""
    source = ('"""Gate — refuses the under-aged."""\n\n'
              'def check(age):\n'
              '    if age < 18:\n'
              '        raise ValueError("under age")\n'
              '    return True\n')
    module = mutate._Module(source)
    assert module.sites
    for index, site in enumerate(module.sites):
        assert mutate._text_of(module, index)[1] is True, site


@pytest.mark.skipif(sys.version_info < (3, 14), reason="t-strings")
def test_a_wrapped_site_after_a_multiline_interpolation_is_spliced():
    """The interpolation's expression spans two lines, and `ast.unparse`
    writes its line break as `\\n`: the endings given back to the span are
    the span's, less the ones the head already carries, or the line count
    is wrong and the whole file is written instead."""
    source = 'def f(a, b, c):\n    return t"""{a <\n b}""" and c\n'
    assert mutate._text_of(mutate._Module(source), 0) == (
        source.replace(" and c", " or c"), True)


def test_a_splice_that_changes_the_line_count_is_not_held(monkeypatch):
    """A splice that parses to the mutant but adds a line moves every line
    after it, so it is not the mutant of the report: the whole file is."""
    module = mutate._Module("x = a < b\ny = 1\n")
    splice = mutate._splice
    monkeypatch.setattr(mutate, "_splice",
                        lambda *args: splice(*args) + "\n")
    assert mutate._text_of(module, 0)[1] is False


@pytest.mark.skipif(sys.version_info < (3, 14), reason="t-strings")
def test_a_whole_file_that_is_not_the_mutant_is_refused(monkeypatch):
    """The file unparsed is checked as the splice is: a mutated tree whose
    interpolation still carries the original text unparses to the original
    program, and that is refused rather than handed to the tests."""
    module = mutate._Module('def f(a, b):\n    return t"{a<b = }"\n')
    (index,) = [i for i, (k, _n, _w) in enumerate(module.sites) if k == "CMP"]
    apply = mutate._apply

    def stale(module, index, base):
        clone = apply(module, index, base)
        if base is module.tree:
            for node in ast.walk(clone):
                if isinstance(node, ast.Interpolation):
                    node.str = "a<b"
        return clone

    monkeypatch.setattr(mutate, "_apply", stale)
    with pytest.raises(mutate.Refusal, match="unparsed is not this mutant"):
        mutate._text_of(module, index)


@pytest.mark.skipif(sys.version_info < (3, 14), reason="t-strings")
def test_a_whole_file_the_parser_rejects_is_refused_not_raised():
    """`ast.unparse` writes a debug field's bare lambda as it was given,
    which 3.14's parser then rejects: that is a refusal like any other file
    that is not its mutant, not a `SyntaxError` out of the run."""
    source = 'def f(a):\n    return t"{(lambda: a < 1)=}"\n'
    with pytest.raises(mutate.Refusal, match="unparsed is not this mutant"):
        mutate._text_of(mutate._Module(source), 0)


def test_not_is_wrapped_so_its_removal_stays_inside_its_span():
    """`not (a or b)` without its `not` is the parenthesised `or` and not the
    bare one `ast.unparse` writes, so the comment and the line numbers around
    it are kept only if `not` is among the wrapped kinds."""
    module = mutate._Module("x = y and not (a or b)  # kept\n")

    assert [w for _k, _n, w in module.sites] == [
        "And -> Or", "not X -> X", "Or -> And"]
    assert mutate._text_of(module, 1) == (
        "x = y and (a or b)  # kept\n", True)


def test_the_raise_operator_really_removes_the_refusal():
    """The operator class that catches a deleted refusal, driven end to end."""
    module = mutate._Module(SAMPLE)
    idx = next(i for i, (k, _n, _w) in enumerate(module.sites) if k == "RAISE")
    mutant = mutate._apply(module, idx, module.tree)
    ns = {}
    exec(compile(mutant, "<mutant>", "exec"), ns)
    # The refusal is gone, so the call that should have raised falls through.
    assert ns["refuse"](1, True, False) is True


#: Every kind, each reached through fields and lists alike: a `not` inside a
#: `BoolOp`, a `raise` in a body inside a body, a compare in an `elif`, and
#: constants in a default, a subscript and a tuple.
NESTED = ("def f(a, b, c=1):\n"
          "    for x in a:\n"
          "        with c:\n"
          "            if x < 0:\n"
          "                raise ValueError(x)\n"
          "            elif b and not x or c[2] >= 3:\n"
          "                try:\n"
          "                    raise KeyError\n"
          "                except (TypeError, KeyError):\n"
          "                    return [y is None for y in (x, True)]\n"
          "    return {k: not v for k, v in a if k != 0}\n")

#: The interpolations whose text `_apply` brings in step, and the ones it
#: leaves: one inside another's expression, three deep with a debug field,
#: a `not` that is an expression whole, a constant in a format spec (whose
#: interpolation's own `x+1` is not the edit's), and an untouched `{x+1}`
#: beside each.
NESTED_TEMPLATE = (
    "def g(a, b, x):\n"
    "    return (t\"{t'{a<b}'} {x+1}\", t\"{x+1:>{3}} {not a}\",\n"
    "            t\"{t'{t\"{x == 1 = }\"}'} {a and not b} {x+1}\")\n")

ALL_NESTED = [NESTED, pytest.param(NESTED_TEMPLATE, marks=pytest.mark.skipif(
    sys.version_info < (3, 14), reason="t-strings"))]


class _Unwrap(ast.NodeTransformer):
    def __init__(self, target):
        self.target = target

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        return node.operand if node is self.target else node


class _ToPass(ast.NodeTransformer):
    def __init__(self, target):
        self.target = target

    def visit_Raise(self, node):
        return ast.Pass() if node is self.target else node


def _apply_to_a_copy(tree, index):
    """Site *index* of *tree* broken in a deep copy of the whole tree, found
    there by a walk of its own, with every interpolation whose expression
    holds the edit given its text back: the mutant `_apply` must make
    without the copy."""
    clone = copy.deepcopy(tree)
    kind, node, _what = mutate._sites(clone)[index]
    if kind == "CMP":
        node.ops[0] = mutate._CMP_SWAP[type(node.ops[0])]()
    elif kind == "BOOL":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif kind == "NOT":
        clone = _Unwrap(node).visit(clone)
    elif kind == "CONST":
        if node.value is True or node.value is False:
            node.value = not node.value
        else:
            node.value = node.value + 1
    elif kind == "RAISE":
        clone = _ToPass(node).visit(clone)
    if sys.version_info >= (3, 14):
        edited = node.operand if kind == "NOT" else node
        for outer in reversed(list(ast.walk(clone))):
            if (isinstance(outer, ast.Interpolation)
                    and any(m is edited for m in ast.walk(outer.value))):
                outer.str = ast.unparse(outer.value)
    return ast.fix_missing_locations(clone)


@pytest.mark.parametrize("source", ALL_NESTED)
def test_a_mutant_is_the_whole_tree_copied_and_broken(source):
    """`_apply` copies the site's path alone, and must make the tree a copy
    of the whole file broken at the site is, from the tree and the twin
    both: a path that leads to another node, an edit made to the wrong copy,
    or an interpolation's text refreshed that holds no edit (the format
    spec's) is a mutant that is not the one the report names."""
    module = mutate._Module(source)
    if source is NESTED:
        assert {k for k, _n, _w in module.sites} == set(mutate.OPERATORS)
    for index in range(len(module.sites)):
        for base in (module.tree, module.twin):
            assert ast.dump(mutate._apply(module, index, base)) == ast.dump(
                _apply_to_a_copy(base, index)), module.sites[index]


@pytest.mark.parametrize("source", ALL_NESTED)
def test_no_mutant_edits_the_trees_every_mutant_is_made_from(source):
    """Every mutant of a file is made from its one tree and its one twin,
    and shares with them every node off its path: an edit of a list or a
    node they hold carries one mutant's edit into each one after it."""
    module = mutate._Module(source)
    before = [ast.dump(t, include_attributes=True)
              for t in (module.tree, module.twin)]
    for index in range(len(module.sites)):
        mutate._text_of(module, index)
        mutate._apply(module, index, module.tree)
    assert [ast.dump(t, include_attributes=True)
            for t in (module.tree, module.twin)] == before


def test_no_mutant_copies_the_whole_tree(monkeypatch):
    """A copy of the whole tree for each mutant is most of what its text
    costs on a large file, so only the site's path is copied."""
    module = mutate._Module(NESTED)
    deepcopy = copy.deepcopy
    given = []

    def spy(x, *args):
        given.append(type(x))
        return deepcopy(x, *args)

    monkeypatch.setattr(copy, "deepcopy", spy)
    for index in range(len(module.sites)):
        mutate._text_of(module, index)
    assert given and ast.Module not in given


def test_a_campaign_walks_its_target_for_sites_once(tree, monkeypatch):
    """The sites, and each one's path, are found once for the campaign: a
    walk for each mutant costs as much as its text on a large file."""
    write_tree(tree, {"pkg/gate.py": NESTED})
    sites = mutate._sites
    walked = []
    monkeypatch.setattr(mutate, "_sites",
                        lambda tree_: walked.append(tree_) or sites(tree_))
    monkeypatch.setattr(mutate, "run_tests",
                        lambda *a, **k: mutate.Verdict(True, 0, "", ""))
    report = mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS, None, None)
    assert report["mutants"] > 1
    assert len(walked) == 1


@pytest.mark.skipif(sys.version_info < (3, 14), reason="t-strings")
def test_a_t_string_mutant_is_compared_with_the_twin():
    """The splice's parse has every interpolation's text normalised, so the
    mutant it is compared with is made from the twin, whose texts are too.
    Made from the tree, the untouched `{x+1}` keeps its raw text, no splice
    of the compare matches it, and the whole file is refused."""
    module = mutate._Module('def f(a, b, x):\n    return t"{a<b} {x+1}"\n')
    (index,) = [i for i, (k, _n, _w) in enumerate(module.sites) if k == "CMP"]
    assert mutate._text_of(module, index) == (
        module.source.replace("a<b", "a <= b"), True)


def test_a_campaign_leaves_the_warning_filters_as_it_found_them(
        tree, monkeypatch):
    """Each mutant's parse is silenced by setting the process's warning
    filters aside and back: a filter left behind would silence, for the rest
    of the process, warnings that are not invective's to hide."""
    monkeypatch.setattr(mutate, "run_tests",
                        lambda *a, **k: mutate.Verdict(True, 0, "", ""))
    filters = warnings.filters
    before = list(filters)
    report = mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS, None, None)
    assert report["mutants"]
    assert warnings.filters is filters and warnings.filters == before


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


def test_a_target_s_own_warnings_are_said_once_and_not_per_mutant(
        repo, monkeypatch):
    """The target is parsed once to find its sites, which says its warnings;
    each mutant's text is parsed again only to be compared, and says nothing,
    since a warning repeated per mutant names no file (`<unknown>:2`) and
    buries the one line a person reads stderr for."""
    write_tree(repo, {"pkg/esc.py": (
        "import re\n"
        "P = re.compile(\"\\d+\")\n"
        "def f(a, b):\n"
        "    if a < b and a > 0:\n"
        "        return 1\n"
        "    return 0\n")})
    monkeypatch.setattr(mutate, "run_tests", lambda *a, **k: mutate.Verdict(
        True, 0, "1 passed", ""))

    with warnings.catch_warnings(record=True) as said:
        warnings.simplefilter("always")
        report = mutate.mutate(repo, os.path.join(repo, "pkg", "esc.py"),
                               GATE_TESTS, ["CMP", "BOOL"], None)

    assert report["mutants"] == 3
    # A `SyntaxWarning` from 3.12, a `DeprecationWarning` before.
    assert len([w for w in said
                if "invalid escape sequence" in str(w.message)]) == 1


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

    # A forwarded plugin comes after invective's own: the plugin notes the
    # modules loaded before it, and one a forwarded plugin imports from
    # outside the copy must not be among them, or it is never named.
    mutate.run_tests("/nowhere", ["t.py"], 1, None, ("-p", "myplugin"))
    argv = seen["argv"]
    assert argv.index("pytest_invective") < argv.index("myplugin"), argv


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
    since the mutant is the file with one span edited. Both are within three
    lines of the end, so the hunk's context runs to the file's last line and
    no further: the file's final line break ends that line, and is no empty
    line after it. The headers spell the path with `/` on every platform,
    while `target` is the path as this platform spells it."""
    report = mutate.mutate(repo, os.path.join(repo, GATE), GATE_TESTS,
                           ["RAISE", "BOOL"], None)
    assert report["target"] == GATE
    count = FILES[GATE.replace(os.sep, "/")].count("\n")
    last = FILES[GATE.replace(os.sep, "/")].split("\n")[-2]

    (kill,) = report["kills"]
    (survivor,) = report["survivors"]
    for entry, out, into in (
            (kill, "        raise ValueError('under age')", "        pass"),
            (survivor, "    if member and age >= 65:",
             "    if member or age >= 65:")):
        # The splice the docstring describes, not the whole-file fallback.
        assert "whole_file" not in entry, entry
        head, body = (entry["diff"].split("\n")[:2],
                      entry["diff"].split("\n")[3:])
        assert head == ["--- pkg/gate.py", "+++ pkg/gate.py (mutant)"]
        assert entry["diff"].split("\n")[2] == "@@ -1,%d +1,%d @@" % (count,
                                                                     count)
        assert body[-1] == " " + last, entry
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
        # The diff is the edited line alone, with no ending left on it.
        diff = entry["diff"].split("\n")
        assert "\r" not in entry["diff"], entry
        assert [x for x in diff[3:] if x[:1] in "-+"] == [
            "-" + lines[entry["line"] - 1].decode(),
            "+" + got[entry["line"] - 1].decode()], entry
    assert [s["source"] for s in report["survivors"]] == [
        "if age < 18:", "raise ValueError('under age')",
        "if member and age >= 65:"]


def test_a_site_the_splice_cannot_hold_falls_back_to_the_whole_file_and_says_so(
        tree, monkeypatch):
    """`pass` padded to the two lines of the `raise` leaves the `; y = 3` on
    a line of its own, which is no statement, so this mutant is the whole
    file unparsed (`ast.unparse` of the mutated tree) -- flagged in its
    entry, since its line numbers are not the file's, and counted in the
    closing lines.
    """
    source = "def f():\n    raise E(\n        1); y = 3\n"
    module = mutate._Module(source)
    (index,) = [i for i, (k, _n, _w) in enumerate(module.sites)
                if k == "RAISE"]
    assert mutate._text_of(module, index) == (
        ast.unparse(mutate._apply(module, index, module.tree)), False)

    write_tree(tree, {"pkg/gate.py": source})
    monkeypatch.setattr(mutate, "run_tests",
                        lambda *a, **k: mutate.Verdict(True, 0, "", ""))
    report = mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS, None, None)

    # The `raise` alone; the two constants beside it are spliced and unflagged.
    assert [(s["kind"], s.get("whole_file")) for s in report["survivors"]] == [
        ("RAISE", True), ("CONST", None), ("CONST", None)]
    # The unparsed file has no final line break and the source does, which
    # is no line taken out.
    assert report["survivors"][0]["diff"].split("\n")[3:] == [
        " def f():", "-    raise E(", "-        1); y = 3", "+    pass",
        "+    y = 3"]
    assert mutate.summary(report)[-1] == (
        "           1 of the mutants could not be written inside their node's "
        "span and were written as a reformatted file")


#: A zero-width space in a string inside an f-string, which `repr` writes
#: as `\\u200b`: before 3.12 a backslash is no part of an f-string's
#: expression, and `ast.unparse` raises `ValueError` rather than write one.
#: The outer compare's span holds the f-string; the inner's is inside it.
UNWRITABLE = "def f(x, y):\n    return f\"{x == 'a\u200bb'}\" < y\n"


def test_a_site_this_python_cannot_write_is_refused_and_not_a_traceback():
    """3.12 and later write both compares inside their spans. Before, the
    outer one's own text cannot be written, and the whole file cannot be
    either, so each site is a `Refusal` and never the `ValueError`."""
    module = mutate._Module(UNWRITABLE)
    assert [k for k, _n, _w in module.sites] == ["CMP", "CMP"]
    for index in range(len(module.sites)):
        if sys.version_info >= (3, 12):
            text, spliced = mutate._text_of(module, index)
            assert spliced and ast.dump(ast.parse(text)) == ast.dump(
                mutate._apply(module, index, module.tree))
        else:
            with pytest.raises(mutate.Refusal) as caught:
                mutate._text_of(module, index)
            assert "ast.unparse cannot write the file" in str(caught.value)


def test_the_command_refuses_a_file_this_python_cannot_write_naming_it(
        tree, monkeypatch, capsys):
    """The refusal reaches the command as one, exit 2, and says the file,
    the line and the change it could not write. From 3.12 `ast.unparse`
    writes this file, so there it is made to fail as 3.11's does."""
    write_tree(tree, {"pkg/gate.py": UNWRITABLE})
    monkeypatch.setattr(mutate, "run_tests",
                        lambda *a, **k: mutate.Verdict(True, 0, "", ""))
    if sys.version_info >= (3, 12):
        def unwritable(node):
            raise ValueError("Unable to avoid backslash in f-string "
                             "expression part")
        monkeypatch.setattr(ast, "unparse", unwritable)
    monkeypatch.chdir(tree)

    assert mutate.main(["--target", GATE, "--tests", *GATE_TESTS]) == 2
    err = capsys.readouterr().err.replace(os.sep, "/")
    assert "refused: pkg/gate.py:2 Lt -> LtE cannot be written" in err


def test_a_test_that_reads_the_module_s_own_source_survives_a_mutant_elsewhere(
        tree):
    """With nothing stubbed: a test that reads the module's source through
    `inspect.getsource` is not failed by a mutant of another function,
    which it would be if the mutant's formatting were `ast.unparse`'s and
    the comment it looks for were gone.
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


def _beside(tmp_path, monkeypatch, files):
    """The fixture project, *files* written over it, outside the directory
    the copies are made in, and the working directory."""
    proj = os.path.realpath(tmp_path / "proj")
    write_tree(proj, {**FILES, **files})
    monkeypatch.chdir(proj)
    return proj


def test_a_settings_file_below_the_top_stops_every_run_s_search_there(
        settings_above_the_copy, tmp_path, monkeypatch, capsys):
    """The tests' own `pytest.ini`, below the top, ends every run's search,
    which starts from the tests' paths and never reaches what is above the
    copy; the project's `pyproject.toml` holds no table that would stop it."""
    _beside(tmp_path, monkeypatch, {
        "pyproject.toml": "[project]\nname = 'p'\n",
        "pkg/tests/pytest.ini": "[pytest]\n"})

    assert mutate.main(["--target", GATE, "--tests", *GATE_TESTS,
                        "--only", "RAISE"]) == 0

    assert "1/1 killed (100.0%)" in capsys.readouterr().out
    assert not settings_above_the_copy.exists()


def test_a_project_without_a_marker_inside_a_repository_is_measured_from_its_top(
        tmp_path, monkeypatch, capsys):
    """A repository's directory is no project's top by itself: started in a
    service of a monorepo that has no marker, the run copies the service, not
    the repository, and its tests import it as they do when run there."""
    no_pytest_settings_above(tmp_path)
    mr = os.path.realpath(tmp_path / "mr")
    api = os.path.join(mr, "api")
    write_tree(mr, {".git/HEAD": "ref: refs/heads/main\n", **MARKERLESS})
    monkeypatch.chdir(api)

    assert config.project_root(api) == api
    assert mutate.main(["--target", "app/gate.py", "--tests", "tests",
                        "--only", "RAISE"]) == 0

    out = capsys.readouterr().out
    assert "project:   %s" % api in out
    assert "1/1 killed (100.0%)" in out


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
                      exclude=("pkg/gate.py",))
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


#: A project laid out as the ones this check is for: a `src/` the top of the
#: copy does not reach, so only something put on `sys.path` can import the
#: package, and tests in a directory with no `__init__.py`, which pytest puts
#: on the path instead of `src`.
SRC_LAYOUT = {
    "src/pkg/__init__.py": "",
    "src/pkg/gate.py": FILES["pkg/gate.py"],
    "tests/conftest.py": "",
    "tests/test_gate.py": FILES["pkg/tests/test_gate.py"],
}

#: The copy's `src` first, as a project whose conftest puts it there does
#: it: inside the copy, the path from the conftest's own file names the
#: copy's `src`.
FIXED_CONFTEST = ("import os\n"
                  "import sys\n"
                  "sys.path.insert(0, os.path.join(os.path.dirname(__file__),"
                  " '..', 'src'))\n")


def _with_on_path(monkeypatch, *entries):
    """`PYTHONPATH` for the runs: this checkout's `src`, so that each run
    loads the plugin from here, and *entries*."""
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([SRC, *entries]))


@pytest.mark.parametrize("fixed", [None, "conftest", "pythonpath"])
def test_a_target_the_tests_import_from_outside_the_copy_is_refused(
        tmp_path, monkeypatch, fixed):
    """A project installed editable has every mutant survive, silently: the
    `.pth` line puts the project's own `src` on `sys.path`, the tests import
    the package from there, and the mutant written in the copy is never
    loaded. A `PYTHONPATH` entry stands in for the `.pth` line here, since
    each lands on the same `sys.path` entry behind the tests directory,
    which is the whole of what the hole needs.

    The refusal names the file the tests loaded and the copy they should
    have loaded from, and the two fixes it offers: with a conftest that
    puts the copy's `src` first, or with pytest's `pythonpath` setting
    naming `src`, the run goes on and the `raise` is killed by the test of
    it.
    """
    project = os.path.realpath(tmp_path / "project")
    write_tree(project, SRC_LAYOUT)
    if fixed == "conftest":
        write_tree(project, {"tests/conftest.py": FIXED_CONFTEST})
    elif fixed == "pythonpath":
        write_tree(project, {"pyproject.toml": "[tool.pytest.ini_options]\n"
                                               "pythonpath = ['src']\n"})
    _with_on_path(monkeypatch, os.path.join(project, "src"))
    target = os.path.join(project, "src", "pkg", "gate.py")

    if fixed is None:
        with pytest.raises(mutate.Refusal) as caught:
            mutate.mutate(project, target, ["tests/test_gate.py"], ["RAISE"],
                          None)
        assert "was imported from %s, not from the copy at " % target in str(
            caught.value)
        assert "invective-" in str(caught.value)
        assert "pytest's `pythonpath` setting naming the directory" in str(
            caught.value)
        # A plugin that imported it first is ahead of the conftest fix, and
        # on a recent pytest behind the setting.
        assert str(caught.value).endswith(
            "A plugin pytest loads before any conftest (an entry point, "
            "PYTEST_PLUGINS, a -p in addopts) that imports it first is ahead "
            "of a conftest; only the `pythonpath` setting, on pytest 8.4 or "
            "later, is ahead of such a plugin.")
        return
    report = mutate.mutate(project, target, ["tests/test_gate.py"], ["RAISE"],
                           None)
    assert report["killed"] == 1
    assert [k["killer"] for k in report["kills"]] == [
        "tests/test_gate.py::test_a_minor_is_refused"]


def test_a_target_imported_under_another_name_from_outside_the_copy_is_refused(
        tmp_path, monkeypatch):
    """A `sys.path` entry inside the package: the tests say `import gate`,
    and nothing is loaded under `pkg.gate`, the name the copy gives the
    target. It is still the project's file, found under the shorter name
    at the target's own layout, `pkg/gate.py`."""
    project = os.path.realpath(tmp_path / "project")
    write_tree(project, SRC_LAYOUT)
    write_tree(project, {"tests/test_gate.py":
                         FILES["pkg/tests/test_gate.py"].replace(
                             "from pkg import gate", "import gate")})
    _with_on_path(monkeypatch, os.path.join(project, "src", "pkg"))
    target = os.path.join(project, "src", "pkg", "gate.py")

    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(project, target, ["tests/test_gate.py"], ["RAISE"], None)
    assert "was imported from %s, not from the copy" % target in str(
        caught.value)


def test_a_loose_target_named_like_a_module_pytest_already_holds_is_not_refused(
        tmp_path, monkeypatch):
    """`platform` is in every pytest process's `sys.modules`, from the
    standard library, under exactly the name the copy gives this file, and
    the tests never load it: they run the file in a child interpreter. The
    standard library's file is not the one the tests loaded the target
    from, and a check that named it would refuse a project for the name of
    one of its scripts."""
    project = os.path.realpath(tmp_path / "project")
    write_tree(project, {
        "tools/platform.py": ("import sys\n"
                              "\n"
                              "def check(n):\n"
                              "    if n < 0:\n"
                              "        raise ValueError('negative')\n"
                              "    return n\n"
                              "\n"
                              "if __name__ == '__main__':\n"
                              "    print(check(int(sys.argv[1])))\n"),
        "tests/test_platform.py": (
            "import subprocess\n"
            "import sys\n"
            "\n"
            "def test_the_count_comes_back():\n"
            "    done = subprocess.run([sys.executable, 'tools/platform.py',"
            " '1'], check=True, capture_output=True, text=True)\n"
            "    assert done.stdout == '1\\n'\n"),
    })
    _with_on_path(monkeypatch)

    report = mutate.mutate(project, os.path.join(project, "tools", "platform.py"),
                           ["tests/test_platform.py"], ["RAISE"], None)

    # The child interpreter does see the mutant; nothing calls `check(-1)`.
    assert (report["mutants"], report["killed"]) == (1, 0)


def test_a_package_named_like_one_of_pytest_s_own_is_not_refused_when_its_tests_never_import_it(
        tmp_path, monkeypatch):
    """Every pytest process holds `_pytest.config`, loaded from
    `_pytest/config/__init__.py`, a file that ends with this target's own
    path. The tests drive the package through a child interpreter only, so
    nothing is loaded under `config`, and nothing is the file to name."""
    project = os.path.realpath(tmp_path / "project")
    write_tree(project, {
        "config/__init__.py": ("def load(x):\n"
                               "    if x < 0:\n"
                               "        raise ValueError('negative')\n"
                               "    return x\n"),
        "tests/test_config.py": (
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            "\n"
            "def test_a_value_is_loaded():\n"
            "    done = subprocess.run([sys.executable, '-c',"
            " 'import config; print(config.load(1))'], cwd=os.getcwd(),"
            " check=True, capture_output=True, text=True)\n"
            "    assert done.stdout == '1\\n'\n"),
    })
    _with_on_path(monkeypatch)

    report = mutate.mutate(project, os.path.join(project, "config", "__init__.py"),
                           ["tests/test_config.py"], ["RAISE"], None)

    assert (report["mutants"], report["killed"]) == (1, 0)


def test_a_ref_in_a_repository_with_no_commit_is_refused_with_git_s_reason(
        tree):
    """There is no HEAD to make a worktree of, and git's words say so."""
    git(tree, "init", "-q", "-b", "main")

    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS, None, None,
                      ref="HEAD")
    assert str(caught.value).startswith("git worktree failed: fatal: ")
    assert "HEAD" in str(caught.value)


def test_a_ref_is_mutated_as_committed_not_as_it_stands(repo):
    """The other way round from a run on the files as they stand."""
    gate = os.path.join(repo, GATE)
    with open(gate, encoding="utf-8") as fh:
        edited = "# an uncommitted line\n" + fh.read()
    write_tree(repo, {"pkg/gate.py": edited})

    report = mutate.mutate(repo, gate, GATE_TESTS, ["RAISE"], None,
                           ref="HEAD")

    assert report["kills"][0]["line"] == 3
    assert len(git(repo, "worktree", "list").splitlines()) == 1


def test_a_ref_from_below_the_top_of_the_repository_is_that_directory(repo):
    """A project can be a directory inside a repository; the copy of the ref
    stands for that directory, not for the repository's top."""
    with git_ref(os.path.join(repo, "pkg"), "HEAD") as where:
        assert sorted(os.listdir(where))[:2] == ["__init__.py", "gate.py"]


@pytest.mark.parametrize("own_pyproject, moved", [
    (True, True), (False, True), (True, False)],
    ids=["moved", "moved-no-pyproject", "kept"])
def test_a_ref_s_settings_above_the_project_in_its_repository_are_read(
        tmp_path, monkeypatch, capsys, own_pyproject, moved):
    """At the ref, pytest's settings are the repository's `pytest.ini`,
    above the project, where its xfail is strict. The ref's runs go by the
    ref's file, which its tree holds, as the ref's own pytest does: the
    gate's one `RAISE` mutant, which only a strict xfail fails, is killed.

    The file may since have moved into the project's own `pyproject.toml`,
    and the ref's project may have a `pyproject.toml` that sets nothing, or
    none at all. Or it is still where it was, as a monorepo keeps it, and is
    above the project as it stands too: a ref's tree holds it all the same,
    so the ref is measured, where a run on the files as they stand is
    refused."""
    no_pytest_settings_above(tmp_path)
    mono = os.path.realpath(tmp_path / "mono")
    old = dict(MONOREPO)
    if not own_pyproject:
        del old["sub/pyproject.toml"]
    ref = monorepo(mono, old)
    if moved:
        os.remove(os.path.join(mono, "pytest.ini"))
        commit(mono, {"sub/pyproject.toml": MONOREPO["sub/pyproject.toml"]
                      + "\n[tool.pytest.ini_options]\nxfail_strict = true\n"})
    monkeypatch.chdir(os.path.join(mono, "sub"))

    assert mutate.main(["--target", "pkg/gate.py", "--tests", "tests",
                        "--only", "RAISE", "--ref", ref]) == 0
    assert "1/1 killed (100.0%)" in capsys.readouterr().out

    if not moved:
        assert mutate.main(["--target", "pkg/gate.py", "--tests", "tests",
                            "--only", "RAISE"]) == 2
        assert ("refused: pytest reads %s, which is above the project's top"
                % os.path.join(mono, "pytest.ini")) in capsys.readouterr().err


def test_settings_above_a_repository_are_in_no_ref_s_tree(
        tmp_path, monkeypatch, capsys):
    """A `pytest.ini` above the repository's top is in no commit of it, so
    a ref's tree never holds it, and every run in one would go without the
    strict xfail it sets: the ref is refused, naming it, and not measured
    with the mutant only that file kills reported as a survivor."""
    no_pytest_settings_above(tmp_path)
    outer = os.path.realpath(tmp_path / "outer")
    mono = os.path.join(outer, "mono")
    write_tree(outer, {"pytest.ini": MONOREPO["pytest.ini"]})
    ref = monorepo(mono, {rel: text for rel, text in MONOREPO.items()
                          if rel != "pytest.ini"})
    monkeypatch.chdir(os.path.join(mono, "sub"))

    assert mutate.main(["--target", "pkg/gate.py", "--tests", "tests",
                        "--only", "RAISE", "--ref", ref]) == 2
    said = capsys.readouterr()
    assert ("refused: pytest reads %s, which is above the top of the "
            "repository %s, so the ref's tree the mutants are run in does not "
            "hold it" % (os.path.join(outer, "pytest.ini"), mono)) in said.err
    assert "copy:" not in said.out


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
    monkeypatch.setattr(process, "stop", stopped.append)

    with pytest.raises(KeyboardInterrupt):
        mutate.run_tests("/nowhere", ["t.py"], timeout=1)
    assert len(stopped) == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows never delivers SIGTERM")
def test_a_terminated_run_stops_its_run_and_removes_its_copy(tree, tmp_path):
    """SIGTERM is what `timeout(1)`, a supervisor and CI's cancel send, and
    without a handler the process dies where it stands: the copy stays, and
    the run goes on in a group of its own. It unwinds as a ^C does instead,
    and exits by the signal, the status a supervisor expects."""
    with slow_run(tree, tmp_path) as (proc, where, run, _parent):
        os.kill(proc.pid, signal.SIGTERM)

        assert proc.wait(timeout=30) == -signal.SIGTERM
        assert not os.path.exists(where)
        with pytest.raises(ProcessLookupError):
            os.killpg(run, 0)


@pytest.mark.skipif(os.name == "nt", reason="Windows never delivers SIGTERM")
def test_a_second_sigterm_while_unwinding_does_not_cut_the_cleanup_short():
    """The first raises, where a ^C would; a second, as the copy is being
    removed, is ignored, or it would raise inside that removal. Afterwards
    the signal has its old handler back."""
    before = signal.getsignal(signal.SIGTERM)
    cleaned = []

    with pytest.raises(process.Terminated) as caught:
        with process.stopping_on_sigterm():
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)
                pytest.fail("the signal did not stop the body")
            finally:
                os.kill(os.getpid(), signal.SIGTERM)
                # A sleep the signal interrupts, so the handler has run by
                # the next line.
                time.sleep(0.05)
                cleaned.append(True)

    assert cleaned == [True]
    assert caught.value.signum == signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.skipif(os.name == "nt", reason="Windows never delivers SIGTERM")
def test_a_sigterm_ignored_on_entry_stays_ignored():
    """A wrapper that ignores SIGTERM (`trap '' TERM`) asked for the run to
    ignore it too, as every other program under it does."""
    old = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        with process.stopping_on_sigterm():
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGTERM, old)


def test_a_previous_handler_python_did_not_install_is_left_alone(monkeypatch):
    """`signal.signal` returns `None` for a handler set from C, and refuses
    `None` as a handler, so putting it back would raise in place of the
    `Terminated` unwinding through the block."""
    calls = []

    def fake(signum, handler):
        calls.append((signum, handler))
        if handler is None:
            raise TypeError("signal handler must be signal.SIG_IGN, "
                            "signal.SIG_DFL, or a callable object")
        return None

    monkeypatch.setattr(process.signal, "signal", fake)
    with pytest.raises(process.Terminated):
        with process.stopping_on_sigterm():
            raise process.Terminated(signal.SIGTERM)
    assert len(calls) == 1 and calls[0][1] is not None, calls


class _Broken(io.StringIO):
    """A stream whose reader has gone: a closed pipe."""

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def test_dying_by_the_signal_first_writes_out_what_was_printed(monkeypatch):
    """A process killed by its own signal never flushes its buffers, so a
    run printing to a file or a pipe would end with that file empty; and a
    stream that cannot be flushed does not stand in for the signal."""
    raw = io.BytesIO()
    # `newline`, so the bytes are what was printed on every platform: by
    # default a text stream on Windows writes each `\n` as `\r\n`.
    out = io.TextIOWrapper(raw, newline="\n")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _Broken())
    out.write("copy: x\n")
    assert raw.getvalue() == b""
    died = []
    monkeypatch.setattr(process.signal, "signal", lambda *a: None)
    monkeypatch.setattr(process.os, "kill", lambda pid, signum: died.append(signum))

    with pytest.raises(SystemExit):
        process.exit_by(process.Terminated(signal.SIGTERM))

    assert raw.getvalue() == b"copy: x\n"
    assert died == [signal.SIGTERM]


def test_a_closed_standard_stream_does_not_stand_in_for_the_signal(
        monkeypatch):
    """Started with stdout closed, a process has `sys.stdout` None, and
    there is nothing to write out: the exit is still by the signal."""
    monkeypatch.setattr(sys, "stdout", None)
    died = []
    monkeypatch.setattr(process.signal, "signal", lambda *a: None)
    monkeypatch.setattr(process.os, "kill", lambda pid, signum: died.append(signum))

    with pytest.raises(SystemExit):
        process.exit_by(process.Terminated(signal.SIGTERM))

    assert died == [signal.SIGTERM]


def test_dying_by_the_signal_is_by_its_default_action(monkeypatch):
    """A handler still in place when the signal is sent would run instead of
    ending the process, so the default is put back first."""
    at_kill = []
    monkeypatch.setattr(process.os, "kill", lambda pid, signum: at_kill.append(
        signal.getsignal(signum)))
    old = signal.signal(signal.SIGTERM, lambda *a: None)
    try:
        with pytest.raises(SystemExit):
            process.exit_by(process.Terminated(signal.SIGTERM))
    finally:
        signal.signal(signal.SIGTERM, old)

    assert at_kill == [signal.SIG_DFL]


def test_a_process_its_own_signal_cannot_end_exits_with_the_signals_status(
        monkeypatch):
    """The kernel ignores a default-action signal a pid namespace's init
    sends itself, so as a container's entry point the self-kill returns.
    The exit is then the status a shell reports for a process the signal
    ended (143 for SIGTERM): not 0, which reads as a passed gate, and not
    a fall through into the report of a run that never finished."""
    killed = []
    monkeypatch.setattr(process.os, "kill", lambda pid, signum: killed.append(
        signum))
    old = signal.signal(signal.SIGTERM, lambda *a: None)
    try:
        with pytest.raises(SystemExit) as caught:
            process.exit_by(process.Terminated(signal.SIGTERM))
    finally:
        signal.signal(signal.SIGTERM, old)

    assert killed == [signal.SIGTERM]
    assert caught.value.code == 128 + signal.SIGTERM == 143


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


def test_a_run_given_its_settings_file_is_not_searched_for_one(tmp_path,
                                                               monkeypatch):
    """`-c` skips pytest's search, so the workspace's settings above the
    member, refused for a search that would reach them, are not read and
    the campaign goes on to its baseline."""
    no_pytest_settings_above(tmp_path)
    top = os.path.realpath(tmp_path / "ws")
    write_tree(top, WORKSPACE)
    member = os.path.join(top, "m")
    runs = []

    def timed_out(where, tests, timeout, selection, options, target):
        runs.append(options)
        return mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT")

    monkeypatch.setattr(mutate, "run_tests", timed_out)

    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(member, os.path.join(member, GATE), ["tests"],
                      ["RAISE"], None)
    assert str(caught.value).startswith("pytest reads %s," % os.path.join(
        top, "pyproject.toml"))
    assert runs == []

    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(member, os.path.join(member, GATE), ["tests"],
                      ["RAISE"], None, options=("-c", "pyproject.toml"))
    assert "took longer than" in str(caught.value)
    assert runs == [("-c", "pyproject.toml")]


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
                  ref="HEAD")

    assert len(git(repo, "worktree", "list").splitlines()) == 1


@pytest.mark.skipif(not hasattr(os, "setsid"), reason="a POSIX session")
def test_a_process_that_left_the_run_s_group_does_not_hold_invective(
        tmp_path, monkeypatch):
    """A process that starts a session of its own escapes the kill, and if
    it holds the run's output open, waiting for that output never ends.

    It holds it only when pytest is not capturing output, which is what the
    `-s` below is for; under capture it inherits a file of pytest's instead.
    """
    monkeypatch.setattr(process, "STOP_GRACE", 0.5)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -s\n",
                                         encoding="utf-8")
    # The escaped process says who it is, so the test can stop what the run
    # by design does not.
    pid_file = tmp_path / "escaped.pid"
    escaped = ("import os, time; open(%r, 'w').write(str(os.getpid())); "
               "time.sleep(30)" % str(pid_file))
    (tmp_path / "test_escape.py").write_text(
        "import os, subprocess, sys, time\n"
        "\n"
        "def test_escape():\n"
        "    subprocess.Popen([sys.executable, '-c', %r], "
        "start_new_session=True)\n"
        "    while not os.path.exists(%r):\n"
        "        time.sleep(0.01)\n"
        "    time.sleep(60)\n" % (escaped, str(pid_file)), encoding="utf-8")

    started = time.monotonic()
    try:
        got = mutate.run_tests(str(tmp_path), ["test_escape.py"], timeout=3)

        assert pid_file.exists(), "the test never got as far as starting it"
        assert got.code == mutate.TIMED_OUT
        assert time.monotonic() - started < 15, "waited on the escaped process"
    finally:
        try:
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
        except (OSError, ValueError):
            pass


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
                      "[tool.pytest.ini_options]\n"
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


def test_a_ref_s_runs_read_the_ref_s_own_settings_file(repo, capsys):
    """The ref's `pytest.ini` makes an xfail strict, which only it fails on
    the mutant; the working tree has moved the setting into
    `pyproject.toml`. Each run in the ref's tree finds the ref's file by
    pytest's own search."""
    commit(repo, {
        "pytest.ini": "[pytest]\nxfail_strict = true\n",
        "pyproject.toml": "[project]\nname = 'p'\n",
        "pkg/tests/test_strict.py": ("import pytest\n"
                                     "from pkg import gate\n"
                                     "\n"
                                     "@pytest.mark.xfail(raises=ValueError)\n"
                                     "def test_a_minor_fails():\n"
                                     "    gate.admit(10, False)\n")})
    old = git(repo, "rev-parse", "HEAD").strip()
    os.remove(os.path.join(repo, "pytest.ini"))
    commit(repo, {"pyproject.toml": ("[project]\nname = 'p'\n"
                                     "[tool.pytest.ini_options]\n"
                                     "xfail_strict = true\n")})

    assert mutate.main(["--target", GATE, "--tests",
                        "pkg/tests/test_strict.py", "--only", "RAISE",
                        "--ref", old]) == 0
    assert "1/1 killed (100.0%)" in capsys.readouterr().out


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
    monkeypatch.chdir(os.path.join(tree, "pkg"))

    assert mutate.main(["--target", "gate.py", "--tests", *tests,
                        "--only", "RAISE"]) == 0
    said = capsys.readouterr().out
    assert "project:   %s" % tree in said
    assert "target:    %s" % GATE in said
    assert "1/1 killed (100.0%)" in said


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="a symbolic link without privileges")
def test_a_target_typed_through_a_link_to_the_project_is_the_project_s(
        tree, tmp_path, monkeypatch, capsys):
    """From a working directory entered through a link, a logical `$PWD`,
    an absolute path to the target leads through the link while the
    project's top is the real directory: it names the project's own file,
    and not one outside it."""
    link = tmp_path / "link"
    os.symlink(tree, link)
    monkeypatch.chdir(link)

    assert mutate.main(["--target", str(link / "pkg" / "gate.py"), "--tests",
                        str(link / "pkg" / "tests" / "test_gate.py"),
                        "--only", "RAISE"]) == 0
    said = capsys.readouterr().out
    assert "target:    %s" % GATE in said
    assert "1/1 killed (100.0%)" in said


def test_a_run_whose_pytest_settings_are_above_the_project_is_refused(
        tmp_path, monkeypatch, capsys):
    """From the member `m` the copy is `m`, which holds none of the settings
    pytest reads from the workspace's top; under its own the xfail is not
    strict, and the mutant that a strict one kills would be reported as a
    survivor. Run from the top, the copy holds them, and it is killed."""
    top = os.path.realpath(tmp_path / "ws")
    write_tree(top, WORKSPACE)
    monkeypatch.chdir(os.path.join(top, "m"))

    assert mutate.main(["--target", os.path.join("pkg", "gate.py"), "--tests",
                        "tests/test_gate.py", "--only", "RAISE"]) == 2
    said = capsys.readouterr()
    assert "refused: pytest reads %s, which is above the project's top %s" % (
        os.path.join(top, "pyproject.toml"), os.path.join(top, "m")) in said.err
    assert "copy:" not in said.out

    monkeypatch.chdir(top)
    assert mutate.main(["--target", os.path.join("m", "pkg", "gate.py"),
                        "--tests", "m/tests/test_gate.py", "--only",
                        "RAISE"]) == 0
    assert "1/1 killed (100.0%)" in capsys.readouterr().out


def test_a_path_is_given_from_the_root_and_what_names_nothing_as_typed(tree):
    """A path that is there is given from the project's root, a path out of
    the project whole, and a node id of a file that is not there, or an
    option, as typed."""
    pkg = os.path.join(tree, "pkg")
    test_gate = os.path.join("pkg", "tests", "test_gate.py")

    assert mutate.rewrite_tests(
        ["tests/test_gate.py::x", "../..", "-q", "tests/test_none.py::x"],
        pkg, tree) == [
        test_gate + "::x", os.path.dirname(tree), "-q", "tests/test_none.py::x"]


def test_the_run_command_line_is_one_a_test_can_parse():
    """`parser()` is `main`'s own parser, built apart from it so a check of
    the documented commands can parse them without running anything."""
    args = mutate.parser().parse_args(
        ["--target", "pkg/gate.py", "--tests", "tests/test_gate.py",
         "tests/test_idle.py", "--only", "RAISE", "--limit", "3",
         "--json", "out.json", "--ref", "main"])

    assert (args.target, args.tests, args.only, args.limit, args.json,
            args.ref) == ("pkg/gate.py",
                          ["tests/test_gate.py", "tests/test_idle.py"],
                          "RAISE", 3, "out.json", "main")


def test_the_tests_given_steer_the_search_for_the_settings_a_run_goes_by(
        tmp_path, monkeypatch):
    """The `pytest.ini` above the project is strict, which the project would
    read from above; the `tests` directory holds a settings file of its own,
    which ends pytest's search there. The campaign is given `tests`, so it is
    not refused for the file above: it is refused only when the runs'
    arguments are left out of the check."""
    top = os.path.realpath(tmp_path)
    no_pytest_settings_above(top)
    write_tree(top, {"pytest.ini": "[pytest]\nxfail_strict = true\n"})
    project = os.path.join(top, "sub")
    write_tree(project, {**SRC_LAYOUT,
                         "pyproject.toml": "[project]\nname = 'p'\n",
                         "tests/conftest.py": FIXED_CONFTEST,
                         "tests/pytest.ini": "[pytest]\n"})
    _with_on_path(monkeypatch, os.path.join(project, "src"))
    target = os.path.join(project, "src", "pkg", "gate.py")

    report = mutate.mutate(project, target, ["tests"], ["RAISE"], None)

    assert report["killed"] == 1
