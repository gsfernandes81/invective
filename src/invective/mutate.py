"""Break the code on purpose and see whether the suite notices.

**Why this exists.** A suite that passes tells you the code does what the
tests say. It does not tell you the tests say anything: a test that expects
`ValueError` from a refusal also passes when the refusal is gone, if some
later line raises the same `ValueError` for an unrelated reason. A test can
pass on a mutant that guts the property it is meant to check.

So this does it mechanically: one small edit at a time, run the tests that are
supposed to be about that file, and report every edit the suite let through. A
surviving mutant is one of two things and both are worth knowing -- a check that
is not load-bearing, or a line that does not matter.

**It runs in a copy of the project, never in the project.** A tool that
rewrites source files in place hands a mutant to whoever else is reading them,
and leaves one behind if it is killed. The copy is of the files as they stand
when the run starts, uncommitted edits included, and is removed at the end,
including after a refusal. `--ref` runs on a commit instead. The one thing a
run writes in the project is `.invective/`, the killer each mutant was last
killed by (`invective.store`), which the copy leaves out.

**A red baseline is a refusal, not a starting point.** If the selection does not
pass on the unmutated tree, every mutant is "killed" for a reason that has
nothing to do with the mutation, and the report reads as a perfect score. So
it is checked before the first mutant runs.

Usage:

    invective run --target src/pkg/gate.py --tests tests/test_gate.py

    invective run --target src/pkg/gate.py \\
        --tests tests/test_gate.py tests/test_gate_edges.py \\
        --only CMP,RAISE --limit 40
"""

from __future__ import annotations

import argparse
import ast
import collections
import contextlib
import copy
import difflib
import functools
import importlib.util
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from collections.abc import Callable
from typing import NamedTuple

from pytest import ExitCode

import pytest_invective
from invective import process, store
from invective.accept import read as read_accepts
from invective.config import (Config, check_pytest_settings,
                              load as load_config, project_root,
                              relative_to_root, settle,
                              workers as workers_of)
from invective.errors import Refusal
from invective.tree import git_ref, head as commit_of, working_tree


# --------------------------------------------------------------------------
# pytest's own exit codes are read by name (`ExitCode`), because *every
# non-zero code is not a kill*. A mutant is killed when a TEST FAILED on it. A
# run that could not be assembled -- a usage error, a selection that
# collected nothing -- is a broken harness, and scoring it as a kill is the
# same vacuous arithmetic the red-baseline refusal exists to prevent,
# arriving one layer down: the score reads high because nothing ran.

#: Not pytest's: a run this module stopped. Any value pytest cannot exit with.
TIMED_OUT = -1  # invective: accept[equivalent: 1 -> 2] any code pytest cannot exit with serves

#: Every code pytest exits with. A run that ended with another, or by a
#: signal, ended outside pytest's say: a kill all the same, with no test to
#: name.
_PYTEST_CODES = frozenset(int(code) for code in ExitCode)

#: How long the unmutated selection may take, so that a suite that hangs ends
#: the campaign instead of holding it for ever.
BASELINE_TIMEOUT = 600  # invective: accept[equivalent: 600 -> 601] any cap far above a suite's time serves

#: The clause a refusal of a target the tests import from outside the copy is
#: known by, here and in the sweep, which reads it in the engine's output.
UNREACHED = "so no mutant of it can reach the tests"


class Verdict(NamedTuple):
    """What one run of the selection said.

    *code* is kept beside *ok* rather than collapsed into it because the
    caller's question is three-valued -- survived, killed, or *do not count
    this run at all* -- and a boolean cannot carry the third.
    """

    ok: bool
    code: int
    tail: str
    #: The node id of the first test that failed, `""` when none did. This is
    #: what makes a campaign's output an oracle rather than a score: a
    #: surviving mutant says a line is unguarded, and a killed one says which
    #: check is doing the guarding.
    killer: str
    #: The node ids of the selection the run could not find.
    missing: tuple[str, ...] = ()
    #: The file the tests loaded the target from when it was not the copy's,
    #: `""` otherwise. A mutant is written in the copy, so a run that says
    #: this is a run no mutant can reach, and its verdict counts for nothing.
    elsewhere: str = ""
    #: The node ids the run kept, in their order, when it was asked for them
    #: (`run_tests`'s *inventory*); empty otherwise.
    selected: tuple[str, ...] = ()


#: pytest can colour its output even when stdout is a pipe, and the tail is
#: read by a person.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


# --------------------------------------------------------------------------
# The vocabulary. Closed, and every entry names the defect it stands for. An
# operator with no sentence beside it is one nobody can judge the survivors of.

OPERATORS = {
    "CMP": "a boundary moved by one: `<=` for `<`, `>=` for `>`, `==` for `!=`",
    "BOOL": "`and` for `or`: a refusal that needed both terms now needs either",
    "NOT": "a `not` dropped from a test: the refusal fires the other way round",
    "CONST": "True for False, or a number off by one",
    "RAISE": "a `raise` replaced by `pass`: THE REFUSAL THAT STOPS REFUSING",
}

_CMP_SWAP = {
    ast.LtE: ast.Lt, ast.Lt: ast.LtE,
    ast.GtE: ast.Gt, ast.Gt: ast.GtE,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}


def _sites(tree: ast.AST) -> list[tuple[str, ast.AST, str]]:
    """Every place this file can be broken, as (kind, node, what changed).

    Collected off the ORIGINAL tree, so a site's reported line number is the
    line somebody can go and look at, and its node's position is the span of
    the source that `_text_of` edits. The mutation itself is applied to a
    copy.
    """
    found: list[tuple[str, ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = type(node.ops[0])
            if op in _CMP_SWAP:
                found.append(("CMP", node,
                              "%s -> %s" % (op.__name__, _CMP_SWAP[op].__name__)))
        elif isinstance(node, ast.BoolOp):
            found.append(("BOOL", node,
                          "%s -> %s" % (type(node.op).__name__,
                                        "Or" if isinstance(node.op, ast.And)
                                        else "And")))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            found.append(("NOT", node, "not X -> X"))
        elif isinstance(node, ast.Constant):
            if node.value is True or node.value is False:
                found.append(("CONST", node,
                              "%s -> %s" % (node.value, not node.value)))
            # Numbers only, and never a string: a mutated string constant is
            # usually a message, and a suite that pins every message would go
            # red on all of them while saying nothing about behaviour.
            elif isinstance(node.value, int) and not isinstance(node.value, bool):
                found.append(("CONST", node, "%d -> %d" % (node.value,
                                                           node.value + 1)))
        elif isinstance(node, ast.Raise):
            found.append(("RAISE", node, "raise ... -> pass"))
    return found


#: 3.14's t-string interpolation, `None` before it.
_INTERPOLATION = getattr(ast, "Interpolation", None)


def _normalised(tree: ast.AST) -> ast.AST:
    """*tree*, with each t-string interpolation's source text rewritten in
    place as `ast.unparse` writes its expression.

    The text is the source's spelling of an expression the tree already
    holds, so two trees that differ only there are the same program up to
    its spacing: a splice of `x+1` to `x+2` parses to the text `x+2`, where
    `_apply` can only give `ast.unparse`'s `x + 2`. An outer interpolation's
    text is written from its inner one's, so the inner is normalised first,
    as `_apply`'s refresh does.
    """
    if _INTERPOLATION is not None:
        for node in reversed(list(ast.walk(tree))):
            if isinstance(node, _INTERPOLATION):
                node.str = ast.unparse(node.value)
    return tree


class _Module:
    """One target, parsed once: its *source*, its *tree*, its *sites*, and
    each site's path from the root as `(field, index)` steps, *index* `None`
    for a field that is not a list (*paths*).

    A path names fields, not nodes, so it leads to the same site in *tree*
    and in *twin*, which is *tree* with every interpolation's text
    normalised (`_normalised`), and is *tree* itself before 3.14. Neither
    is ever edited: every mutant of the file is made from them.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.tree = ast.parse(source)
        self.sites = _sites(self.tree)
        at = {id(node): n for n, (_kind, node, _what) in enumerate(self.sites)}
        # `None` until the walk reaches the site: a site it never reached
        # fails in `_apply`, where an empty path would edit the root instead.
        self.paths: list = [None] * len(self.sites)
        todo: list[tuple[ast.AST, tuple]] = [(self.tree, ())]
        while todo:
            node, path = todo.pop()
            if id(node) in at:
                self.paths[at[id(node)]] = path
            for field, value in ast.iter_fields(node):
                if isinstance(value, ast.AST):
                    todo.append((value, path + ((field, None),)))
                elif isinstance(value, list):
                    todo.extend((item, path + ((field, i),))
                                for i, item in enumerate(value)
                                if isinstance(item, ast.AST))
        self.twin = (self.tree if _INTERPOLATION is None
                     else _normalised(copy.deepcopy(self.tree)))


def _apply(module: _Module, index: int, base: ast.AST) -> ast.AST:
    """*base*, the module's tree or its twin, with site *index* broken and
    nothing else touched.

    Only the site's path is copied, and nothing *base* holds is edited:
    every other mutant of the file is made from it too. The mutant shares
    every node off that path with *base*, so it is never edited either. The edits here and
    in `_mutated_node` are the same by construction, one made in the whole
    tree and one in the node alone, and must stay so: `_text_of`'s
    comparison of the two trees is what catches a drift, by writing every
    mutant as the whole file.
    """
    kind = module.sites[index][0]
    path = module.paths[index]
    held = [base]
    for field, at in path:
        value = getattr(held[-1], field)
        held.append(value if at is None else value[at])
    node = held.pop()
    if kind == "CMP":
        new = copy.copy(node)
        new.ops = [_CMP_SWAP[type(node.ops[0])]()]             # type: ignore[attr-defined]
    elif kind == "BOOL":
        new = copy.copy(node)
        new.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()   # type: ignore[attr-defined]
    elif kind == "NOT":
        # A UnaryOp cannot become its own operand in place, so the operand
        # takes its slot in the parent.
        new = node.operand                                     # type: ignore[attr-defined]
    elif kind == "CONST":
        new = copy.copy(node)
        if node.value is True or node.value is False:          # type: ignore[attr-defined]
            new.value = not node.value                         # type: ignore[attr-defined]
        else:
            new.value = node.value + 1                         # type: ignore[attr-defined]
    elif kind == "RAISE":
        new = ast.copy_location(ast.Pass(), node)
    # Each ancestor is copied with its slot on the path replaced, and a list
    # holding that slot is copied first: *base*'s own list would carry the
    # edit into every mutant made after this one.
    for owner, (field, at) in zip(reversed(held), reversed(path)):
        parent = copy.copy(owner)
        if at is None:
            setattr(parent, field, new)
        else:
            items = list(getattr(parent, field))
            items[at] = new
            setattr(parent, field, items)
        # **A t-string's interpolation carries its expression's source text**
        # (3.14's `Interpolation.str`, the template's `.expression` at run
        # time), which the parser fills and `ast.unparse` writes the
        # interpolation back from. An edit inside one leaves that text naming
        # the original expression, so it is brought in step for every
        # interpolation whose expression holds the edit, and for no other:
        # an untouched `{x+1}` keeps its own text, as the splice keeps it,
        # and so does one whose format spec alone holds the edit. The copies
        # are made from the site up, so an outer interpolation's text is
        # written from an inner one's already brought in step.
        if (_INTERPOLATION is not None and field == "value"
                and isinstance(parent, _INTERPOLATION)):
            parent.str = ast.unparse(parent.value)
        new = parent
    return new


# --------------------------------------------------------------------------
# **A mutant is the file with one span edited, not the file unparsed.** The
# whole file through `ast.unparse` drops every comment, re-wraps every line
# and moves every line number: a traceback in a killed run points at lines
# the source does not have, a diff of the mutant is the whole file, and a
# test that reads the module's own source (`inspect.getsource`, a check on a
# file's text, a line-number assertion) fails on every mutant for the
# formatting and not the mutation, a false kill of all of them. So only the
# site's span is rewritten, and the rest of the file is its own text.

#: The three endings Python's tokenizer counts as a line break, and nothing
#: else: not `str.splitlines`, which also splits on `\f`, `\v`, `\x1c`-`\x1e`,
#: `\x85` and `\u2028`, none of which moves a `lineno`. Captured, so a split
#: gives the lines at the even indexes and their endings at the odd ones.
_LINE_END = re.compile(r"(\r\n|\r|\n)")


def _report_lines(text: str) -> list[str]:
    """*text*'s lines as the report numbers them, without their endings.

    A file that ends in a line break splits into one more piece than it has
    lines, an empty one after the last break, which is no line of the file:
    a diff would show it as a context line the file does not have, or as a
    blank line taken out where `ast.unparse` writes no final break.
    """
    parts = _LINE_END.split(text)[::2]
    return parts[:-1] if parts and parts[-1] == "" else parts


#: The kinds whose replacement is an expression. Parentheses make one safe
#: whatever its parent's precedence, which the bare text is not always: the
#: inner `and` of `a and b or c` turned to `or` must read `(a or b) or c`,
#: since `a or b or c` is one flat `or`, a different tree with the same
#: meaning. Bare first all the same, so the usual survivor's diff reads
#: `if member or age >= 65:` and not `if (member or age >= 65):`. A `CONST`
#: is never wrapped: a literal binds tightest already, and a parenthesised
#: key in a `case {1: x}` pattern is a syntax error. `RAISE` is a statement.
_WRAPPED = frozenset(("CMP", "BOOL", "NOT"))


def _mutated_node(module: _Module, index: int) -> tuple[str, ast.AST, ast.AST]:
    """Site *index*: its kind, its node in the module's tree, and a copy of
    that node alone with the kind's edit made to it.

    The edits here and in `_apply` are the same by construction and must
    stay so: `_text_of`'s comparison of the two trees is what catches a
    drift, by writing every mutant as the whole file.
    """
    kind, node, _what = module.sites[index]
    edited = copy.deepcopy(node)
    if kind == "CMP":
        edited.ops[0] = _CMP_SWAP[type(edited.ops[0])]()        # type: ignore[attr-defined]
    elif kind == "BOOL":
        edited.op = ast.Or() if isinstance(edited.op, ast.And) else ast.And()   # type: ignore[attr-defined]
    elif kind == "NOT":
        edited = edited.operand                                # type: ignore[attr-defined]
    elif kind == "CONST":
        if edited.value is True or edited.value is False:      # type: ignore[attr-defined]
            edited.value = not edited.value                    # type: ignore[attr-defined]
        else:
            edited.value = edited.value + 1                    # type: ignore[attr-defined]
    elif kind == "RAISE":
        edited = ast.Pass()
    return kind, node, edited


def _splice(source: str, node: ast.AST, head: str, tail: str = "") -> str:
    """*source* with the span of *node* replaced by *head*, then the line
    endings the span held that *head* does not, then *tail*.

    The endings are given back so that every line after the node keeps its
    number: between *head* and *tail*, which is inside the parenthesis of a
    wrapped expression, where a line break is a continuation, and after
    `pass`, since a blank line is legal at any indentation. They are the
    span's own endings, not the file's first, given back for the line breaks
    *head* lacks; a line break *head* carries itself is `\\n` whatever the
    file's ending, as in a 3.14 interpolation whose expression spans lines,
    which `ast.unparse` writes from the source text the tokenizer stored.

    Positions are the parser's: `col_offset` and `end_col_offset` count
    BYTES of the UTF-8 line, so the edit is made on bytes. `end_col_offset`
    never reaches a `\\r` of a `\\r\\n` ending: the tokenizer had translated
    it before the positions were assigned.
    """
    pieces = _LINE_END.split(source)
    lines, ends = pieces[::2], pieces[1::2]
    starts = [0]
    for line, end in zip(lines, ends):
        starts.append(starts[-1] + len(line.encode("utf-8")) + len(end))
    first, last = node.lineno, node.end_lineno                 # type: ignore[attr-defined]
    a = starts[first - 1] + node.col_offset                    # type: ignore[attr-defined]
    b = starts[last - 1] + node.end_col_offset                 # type: ignore[attr-defined]
    kept = "".join(ends[first - 1 + head.count("\n"):last - 1])
    data = source.encode("utf-8")
    return (data[:a] + (head + kept + tail).encode("utf-8") + data[b:]
            ).decode("utf-8")


def _reparsed(text: str) -> ast.AST:
    """*text* parsed only to be compared, silently: a warning it raises is
    the target's own, which the parse that found its sites has said once.
    While it runs, no other thread may warn or change the warning filters:
    the filters it sets aside are the process's (unless
    `sys.flags.context_aware_warnings`), so another thread's warning would
    be lost and its new filter undone."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(text)


def _text_of(module: _Module, index: int) -> tuple[str, bool]:
    """The mutant's text, and whether it is the module's source edited
    inside the site's span only (True) or the whole file unparsed (False).

    **The splice is checked, not trusted.** Its result is parsed and
    compared, as a tree, with the mutant `_apply` makes, and must have the
    source's line count: so no quirk of a Python version's positions or of
    `ast.unparse` can hand the tests a mutant that is valid and not the one
    the report describes. When the bare text fails that, an expression is
    tried once more in parentheses; when that fails too, or for any other
    exception from the attempt (an out-of-range position, a splice that
    does not parse, a null byte's `ValueError`), the mutant is the whole
    file unparsed (`ast.unparse` of the mutated tree), and the report says
    so beside it. A refusal would stop a campaign for one awkward site, and
    skipping the site would make the count of mutants depend on the
    interpreter. The whole file is checked the same way, and one that does
    not parse back to the mutant is a `Refusal`: written anyway, it would
    be another program than the report names, most likely the original.

    **Before 3.12 `ast.unparse` cannot write some f-strings at all**: a
    string holding a character `repr` escapes, inside an f-string's
    expression, raises `ValueError`, since that grammar has no backslash
    there. The node's own text is then not tried, and when the whole file
    cannot be written either, this is a `Refusal`: there is no text of this
    mutant to hand the tests.
    """
    source = module.source
    kind, node, edited = _mutated_node(module, index)
    # Made from the twin, whose interpolation texts are normalised as each
    # parse compared with it is below.
    want = ast.dump(_apply(module, index, module.twin))
    forms = []
    try:
        text = ast.unparse(edited)
    except ValueError:
        pass
    else:
        forms.append((text, ""))
        if kind in _WRAPPED:
            forms.append(("(" + text, ")"))
    for head, tail in forms:
        try:
            out = _splice(source, node, head, tail)
            if (ast.dump(_normalised(_reparsed(out))) == want
                    and len(_LINE_END.split(out)) == len(_LINE_END.split(source))):
                return out, True
        except Exception:
            pass
    # Made from the tree, not the twin, so an interpolation the edit is not
    # in is written from its own text, as the splice keeps it.
    whole = _apply(module, index, module.tree)
    try:
        text = ast.unparse(whole)
    except ValueError as exc:
        raise Refusal("cannot be written inside its node's span, and this "
                      "Python's ast.unparse cannot write the file: %s" % exc
                      ) from exc
    try:
        same = ast.dump(_normalised(_reparsed(text))) == want
    except (SyntaxError, ValueError):
        # `ast.unparse` can write what the parser then rejects: 3.14's
        # t-string debug field around a bare lambda.
        same = False
    if not same:
        raise Refusal("cannot be written inside its node's span, and the "
                      "file unparsed is not this mutant")
    return text, False


# --------------------------------------------------------------------------


def _write(path: str, text: str, when: int) -> None:
    """Write *text*, then stamp the file's mtime at *when*.

    The stamp is the point. See the loop in `mutate` for why a whole second
    apart is the unit: CPython's bytecode cache keys on the source's mtime in
    seconds plus its size, and two mutants are often the same size.

    **`newline=""`, because the default rewrites every line ending on
    Windows.** Text mode translates `\\n` to `\\r\\n` there, so a mutant
    differs from its original at every line in the file and not only at the
    mutation -- and the size this function is so careful about is a byte per
    line larger than the source it was built from. POSIX never shows it: text
    mode translates nothing there.
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.utime(path, (when, when))


#: No pytest-xdist workers: a repository that turns a pool on would start
#: one per MUTANT, for a selection a single process runs faster. Where xdist
#: is installed, `-n 0` says so and also wins over an `-n` in the
#: repository's own `addopts`, which `-p no:xdist` would leave as an option
#: nothing knows. Where it is not, `-n` is no option at all.
if importlib.util.find_spec("xdist") is not None:
    _NO_WORKERS = ["-n", "0"]
else:
    _NO_WORKERS = ["-p", "no:xdist"]


class Stopped(Exception):
    """A run cut short because the campaign is stopping. Raised in place of
    a verdict, so that a run that never finished is never one."""


#: How often a run in a worker looks for the campaign stopping, in seconds.
_POLL = 0.2


def _wait(proc: subprocess.Popen, timeout: float,
          stop: threading.Event | None) -> tuple[str, str]:
    """What *proc* printed, once it has ended within *timeout*; raises
    `subprocess.TimeoutExpired` when it has not, and `Stopped` as soon as
    *stop* is set, leaving *proc* running for the caller to stop."""
    if stop is None:
        return proc.communicate(timeout=timeout)
    end = time.monotonic() + timeout
    while True:
        # First right after the start: a stop set while the run was being
        # started is seen before anything is waited for.
        if stop.is_set():
            raise Stopped
        try:
            # A worker waits on its run out of the hush, which is what lets
            # the main thread make texts while the runs go on.
            with _Hush.aside():
                return proc.communicate(
                    timeout=max(0.0, min(_POLL, end - time.monotonic())))
        except subprocess.TimeoutExpired:
            # Asked again, `communicate` loses nothing it had read.
            if time.monotonic() >= end:
                raise


def run_tests(where: str, tests: list[str], timeout: float,
              selection: str | None = None, options: tuple[str, ...] = (),
              target: str = "", stop: threading.Event | None = None, *,
              inventory: bool = False) -> Verdict:
    """The selection, in *where*: *tests* as pytest arguments, and when
    *selection* names a file of node ids, one a line, those tests and no
    others, *tests* then deciding only where pytest's search for its
    settings starts. *options* come after `-p pytest_invective` and before
    the rest of invective's own: the plugin notes which modules were loaded
    before it, and a forwarded `-p` plugin that imports the target has to
    come after that note for the file it loaded to be the plugin's to name.
    *target*, the mutated module's path from the top of *where*, is what the
    plugin checks the tests loaded from the copy; none, and it checks
    nothing. Once *stop* is set, the run is stopped with everything it
    started and `Stopped` is raised: a signal reaches only the main thread,
    and a run in a worker's thread learns of it this way. With *inventory*,
    the verdict lists the tests the run kept (`Verdict.selected`).

    **No `-q` here, and that is load-bearing.** The run's working directory is
    the copy, so it reads the project's own pytest
    configuration, whose `addopts` may already carry one -- and a second
    STACKS into `-qq`, under which pytest prints neither a summary line nor a
    single node id, and the `tail` a person reads after a refusal says
    nothing. `-rf` is the other half: under plain `-q` the short summary is
    suppressed too, so the `FAILED <nodeid>` lines have to be asked for.

    **The killer comes from the plugin, not from the printed output.** The
    run loads `pytest_invective`, which writes the first failing node id to
    the file `INVECTIVE_VERDICT` names, so a node id is never cut out of
    text that colour, spaces and pytest's own separators can break up.
    """
    with tempfile.TemporaryDirectory(prefix="invective-verdict-") as box:
        verdict = os.path.join(box, "verdict.json")
        env = {**os.environ, pytest_invective.VERDICT: verdict}
        if selection is not None:
            env[pytest_invective.SELECTION] = selection
            env[pytest_invective.TYPED] = str(len(tests))
        if target:
            env[pytest_invective.TARGET] = target
        if inventory:
            env[pytest_invective.INVENTORY] = "1"
        # `-p no:randomly` keeps the order, and so `-x`'s first failure, the
        # same from run to run; `-p no:` of a plugin that is not installed is
        # a no-op. The `no:` exclusions and `-n 0` are order-free: pytest
        # applies a `no:` before it imports anything.
        if stop is not None and stop.is_set():
            raise Stopped
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-p", pytest_invective.__name__,
             *options, "-x", "-rf", "-p", "no:randomly", *_NO_WORKERS,
             "--no-header", *tests],
            cwd=where, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, **process.OWN_GROUP)
        try:
            stdout, stderr = _wait(proc, timeout, stop)
        except subprocess.TimeoutExpired:
            # Out of the hush, as the wait was: the stop waits on the run
            # too, up to `process.STOP_GRACE` when something it started
            # holds its output, and a text can be made meanwhile.
            with _Hush.aside():
                process.stop(proc)
            # A mutant that hangs is a mutant the suite noticed, in the least
            # helpful way available. Counted as killed and said out loud,
            # because a timeout that is silently a pass would flatter the
            # score.
            return Verdict(False, TIMED_OUT, "TIMEOUT", "TIMEOUT")
        except BaseException:
            # Interrupted, or the campaign is stopping: the run is in a
            # group of its own, so the ^C that stopped this process never
            # reached it.
            process.stop(proc)
            raise
        try:
            with open(verdict, encoding="utf-8") as fh:
                said = json.load(fh)
            killer, missing = said["killer"], tuple(said["missing"])
            # `.get`: the field may be absent from the verdict of a run cut
            # short, and the inventory is there only when it was asked for.
            elsewhere = said.get("elsewhere", "")
            selected = tuple(said.get("selected", ()))
        except (OSError, ValueError, KeyError):
            # A run that ended before its session did -- pytest could not
            # start, or a mutant broke the plugin itself -- names no test.
            killer, missing, elsewhere, selected = "", (), "", ()
    # Both streams: pytest says why it could not start on stderr.
    out = _ANSI.sub("", stdout + stderr)
    # invective: accept[equivalent: 400 -> 401] any length that holds pytest's last words serves
    return Verdict(proc.returncode == 0, proc.returncode, out[-400:], killer,
                   missing, elsewhere, selected)


# --------------------------------------------------------------------------
# **Workers.** Each has a copy of the project of its own and a thread that
# runs in it, and every run of the campaign is made on one, the baselines and
# confirmations too. The main thread makes every mutant's text (`_text_of`
# sets the process's warning filters aside, so it runs there alone, with
# every worker held off by `_Hush`), hands out the work, and reads, confirms
# and reports what it came to, in the order of the sites. At one worker it is
# a serial campaign.


class Job(NamedTuple):
    """One mutant of the campaign: its place among them, from 1, which is
    also the second its write is stamped at (*n*); its site's index in the
    module (*idx*); and its kind, change and line."""

    n: int
    idx: int
    kind: str
    what: str
    line: int


class Mutant(NamedTuple):
    """A job and the text written for it, made on the main thread:
    *spliced* is False when the text is the whole file unparsed."""

    job: Job
    text: str
    spliced: bool


class Outcome(NamedTuple):
    """What a mutant came to, and how.

    *via* names the attempt that decided it, `""` for the whole selection,
    and *origin* what decided a verdict that was read back rather than run.
    *confirmed* says how a kill made while other copies' runs went on was
    confirmed with nothing else running: `"alone"`, its killer failed alone,
    or `"full"`, the whole selection was run again and decided it. The
    verdict is then that run's, so a survivor carries it too."""

    verdict: Verdict
    via: str = ""
    origin: str = ""
    confirmed: str = ""


#: A way to measure a mutant before the whole selection: a kill, or None to
#: leave the mutant to the next way, and in the end to the whole selection.
Attempt = Callable[[Mutant, "Copy"], "Outcome | None"]

#: What `unreproduced` says of a kill no killer could be run alone to
#: confirm, a kill by time, by a module or under tests that hold an option,
#: when the whole selection run again with nothing else running lets the
#: mutant through.
NO_KILLER = "no killer to run alone"

#: The only ways a survivor is reached. A survivor is a claim about the whole
#: selection, so only a run of all of it, or such a verdict read back, can
#: make one: an attempt that runs fewer tests can kill a mutant, never let
#: one through.
_SURVIVOR_VIA = frozenset({"", "cache"})


class Copy:
    """One copy of the project the campaign's mutants are written in, and
    the only way a run is made in it (`run`).

    **Each copy's writes are stamped at rising seconds.** In the pool, a
    mutant is stamped at the campaign's clock plus its place, and a copy is
    given its mutants in the order of their places; once the copy is
    restored (`restore`), each write takes the next second past the last
    place. See the loop in `mutate` for why.
    """

    def __init__(self, where: str, rel: str, source: str, tests: list[str],
                 listed: str | None, options: tuple[str, ...],
                 stop: threading.Event, count: int) -> None:
        self.where, self.rel, self.source = where, rel, source
        self.path = os.path.join(where, rel)
        self.tests, self.listed, self.options = tests, listed, options
        self.stop, self.count = stop, count
        #: The campaign's clock, set before the first mutant is written:
        #: a write before that raises, rather than stamp a second that can
        #: be older than the copy's own file.
        self.clock: int | None = None
        # The mutant the file may hold, None while it is the original's.
        self._held: Mutant | None = None
        # The next stamp once restored; None while in the pool.
        self._next: int | None = None

    def run(self, mutant: Mutant | None, timeout: float,
            selection: str | None = None, **handshake) -> Verdict:
        """The selection run on *mutant*, written first unless it is the
        mutant written last, or with None on the original, which a copy
        holds only before its first mutant and once restored. *selection*
        names a file of node ids to run instead of the campaign's;
        *handshake* goes to `run_tests` as it is.

        **The tests have to have loaded the copy's file**, or no mutant
        written there reaches them: a run that says otherwise is a
        `Refusal`, whichever run it is.
        """
        if mutant is None:
            if self._held is not None:
                raise RuntimeError("a run of the original in %s, which holds "
                                   "the mutant of line %d"
                                   % (self.where, self._held.job.line))
        elif mutant is not self._held:
            # Held before it is written: a write cut short leaves the file
            # neither, and a run of the original must not follow it.
            self._held = mutant
            _write(self.path, mutant.text, self._stamp(mutant.job.n))
        got = run_tests(self.where, self.tests, timeout,
                        self.listed if selection is None else selection,
                        self.options, self.rel.replace(os.sep, "/"),
                        self.stop, **handshake)
        if got.elsewhere:
            # **The tests did not load the copy's file, so no mutant
            # written there can reach them**, and every one would
            # survive, silently. An editable install's `.pth` line, or a
            # `sys.path` entry, puts the project's own `src` on the path;
            # the copy's is there only when something puts it there,
            # since `python -m pytest` adds the directory it started in,
            # the copy's top, and that does not reach a `src/`. pytest's
            # `pythonpath` setting does: each run reads it from the
            # settings file in the copy, and so from the copy's rootdir,
            # and pytest 8.4 and later apply it before loading any
            # plugin, which a conftest comes after. The baseline is
            # where this fires; every run is checked because the check
            # is one field and this is the one place it is read.
            raise Refusal((
                "%s was imported from %s, not from the copy at %s, "
                + UNREACHED + ". An editable install, "
                "or a sys.path entry, points the tests at the project "
                "itself; pytest's `pythonpath` setting naming the "
                "directory the package is in (`src`), or a conftest.py "
                "that puts the directory of the copy's own `src` first "
                "on sys.path, computed from its own __file__, makes them "
                "import the copy. A plugin pytest loads before any "
                "conftest (an entry point, PYTEST_PLUGINS, a -p in "
                "addopts) that imports it first is ahead of a conftest; "
                "only the `pythonpath` setting, on pytest 8.4 or later, "
                "is ahead of such a plugin.")
                % (self.rel, got.elsewhere, self.where))
        return got

    def restore(self) -> None:
        """Write the original back, so the copy can run it again."""
        _write(self.path, self.source, self._stamp(None))
        self._held = None

    def _stamp(self, n: int | None) -> int:
        if self._next is None and n is not None:
            return self.clock + n
        if self._next is None:
            self._next = self.clock + self.count + 1
        when, self._next = self._next, self._next + 1
        return when


class _Hush:
    """Workers held off while the main thread makes a mutant's text.

    `_reparsed` sets the process's warning filters aside, so while it runs
    no other thread may warn: its warning would be lost, as an ignored one.
    A worker could warn anywhere in its task (a `ResourceWarning` from an
    object it lets go, a `DeprecationWarning` from a library, whatever an
    attempt runs), so rather than hold every one of those to never warn,
    each worker's task runs inside `shared`, which any number of workers
    hold at once, and the main thread makes a text inside `alone`, which
    waits until no worker holds `shared` and keeps new ones out. A worker
    steps `aside` only while it waits for its run's process to end, which
    warns nothing, and is where it spends its time: so the texts are still
    made while the runs go on.

    A worker held off gives up once *stop* is set, raising `Stopped`, so a
    halt never waits on a text; the main thread, held off, calls *check*,
    which raises a signal held meanwhile (`_Held.check`).
    """

    # The hush a thread holds `shared`, if any, for `aside` to find.
    _held = threading.local()

    def __init__(self, stop: threading.Event | None = None,
                 check: Callable[[], None] | None = None) -> None:
        self._turn = threading.Condition()
        self._sharing = 0
        self._alone = False
        self._asking = 0
        self._stop, self._check = stop, check

    def _wait(self, held_off: Callable[[], bool], worker: bool) -> None:
        # In short spells, so that a ^C reaches the main thread on Windows
        # and a stop reaches a worker.
        while held_off():
            if not worker:
                if self._check is not None:
                    self._check()
            elif self._stop is not None and self._stop.is_set():
                raise Stopped
            self._turn.wait(_POLL)

    @contextlib.contextmanager
    def shared(self):
        with self._turn:
            # A text asked for goes first: workers never starve it.
            self._wait(lambda: self._alone or self._asking, worker=True)
            self._sharing += 1
        _Hush._held.hush = self
        try:
            yield
        finally:
            # Not when `aside` gave the hush up on a stop: it is not held.
            if getattr(_Hush._held, "hush", None) is self:
                _Hush._held.hush = None
                with self._turn:
                    self._sharing -= 1
                    self._turn.notify_all()

    @contextlib.contextmanager
    def alone(self):
        held = False
        try:
            with self._turn:
                self._asking += 1
                try:
                    self._wait(lambda: self._alone or self._sharing,
                               worker=False)
                finally:
                    self._asking -= 1
                # Inside the `try`: an exception as the lock is let go
                # must not leave the workers held off for ever.
                self._alone = held = True
            yield
        finally:
            if held:
                with self._turn:
                    self._alone = False
                    self._turn.notify_all()

    @staticmethod
    @contextlib.contextmanager
    def aside():
        """Out of the hush this thread holds `shared`, if any, for the
        body, and back in after it, unless the campaign is stopping: then
        `Stopped`, and the hush is no longer held."""
        hush = getattr(_Hush._held, "hush", None)
        if hush is None:
            yield
            return
        with hush._turn:
            hush._sharing -= 1
            hush._turn.notify_all()
        _Hush._held.hush = None
        try:
            yield
        finally:
            with hush._turn:
                hush._wait(lambda: hush._alone or hush._asking, worker=True)
                hush._sharing += 1
            _Hush._held.hush = hush


#: The signals a handler of invective's can be standing in for while the
#: workers run: a ^C, and the SIGTERM `process.stopping_on_sigterm` raises.
_HELD = (signal.SIGINT, signal.SIGTERM)


class _Held:
    """SIGINT and SIGTERM held, from the start of the workers to their end:
    each is noted where it lands, and raised where the main thread asks
    for it (`check`), or once the workers have ended (`end`), by calling
    the handler that was there, so it raises what it would have raised.

    **A signal raises on the main thread wherever it is running Python**,
    and on 3.12 and later that includes the start of a lock's `__exit__`.
    A ^C landing there, after the body of a `with` on a
    `threading.Condition` (the hush's, a queue's, an event's) and before
    the release, leaves the lock held, and a worker that then needs it, to
    post, to come back from `aside` or to end, waits for ever, and the halt
    with it. Raised where the main thread asks, a signal finds no lock held.

    Only a handler Python runs is held, and only on the main thread, the
    one a handler can be set from: a signal ignored, or ending the process
    by default, is left as it is, and another thread is reached by none.

    **A SIGTERM goes before a ^C**, and once one has been raised, a ^C
    noted after it is not: a SIGTERM is a supervisor's, and the process
    then ends by it (143 in a shell), which a ^C raised in its place, or
    over it, would turn into an interrupt.
    """

    def __init__(self) -> None:
        self._saved: dict = {}
        self._noted: collections.deque = collections.deque()
        #: Whether a SIGTERM has been raised.
        self._terminated = False
        try:
            for signum in _HELD:
                handler = signal.getsignal(signum)
                if not callable(handler):
                    continue
                # Saved first: one put back that was never replaced is
                # put back as it was.
                self._saved[signum] = handler
                try:
                    signal.signal(signum, self._note)
                except ValueError:
                    del self._saved[signum]
                    break
        except BaseException:
            self.end()
            raise

    def _note(self, signum: int, frame) -> None:
        self._noted.append((signum, frame))

    def check(self) -> None:
        """Raise what a signal noted would have raised where it landed."""
        self._raise(self._saved)

    def end(self) -> None:
        """Put each handler back, then raise what a signal noted meanwhile
        would have raised."""
        saved, self._saved = self._saved, {}
        for signum, handler in saved.items():
            signal.signal(signum, handler)
        self._raise(saved)

    def _raise(self, saved: dict) -> None:
        while self._noted:
            terms = [note for note in self._noted
                     if note[0] == signal.SIGTERM]
            signum, frame = terms[0] if terms else self._noted[0]
            self._noted.remove((signum, frame))
            if signum == signal.SIGINT and self._terminated:
                continue
            if signum == signal.SIGTERM:
                self._terminated = True
            saved[signum](signum, frame)


class _Pool:
    """A thread for each copy, the only one to run in it, started at once
    and ended by `close`, or by `halt` when the campaign is stopping.

    The main thread gives a worker a task in the worker's own inbox
    (`give`), and reads what each task came to, its result or what it
    raised, from one queue they all post to (`take`). A run's process is
    touched only by the thread that started it, and a signal is delivered
    to the main thread only, which tells the runs by the *stop* event.

    From the start to `close` or `halt`, SIGINT and SIGTERM are held
    (`_Held`), and raised where the main thread waits (`take`, and a text
    held off by the hush).

    **No run is started on the main thread.** A ^C that lands inside
    `subprocess.Popen` there, once the process is made and before it is
    handed back, leaves a run that nothing can stop, going on in a copy
    about to be removed. On a worker, the ^C lands on the main thread, which
    sets *stop*, and the worker stops the run it started.
    """

    def __init__(self, copies: list[Copy], stop: threading.Event,
                 say: Callable[[str], object]) -> None:
        self.copies, self.stop, self.say = copies, stop, say
        #: Before any worker starts: a signal is held from then on.
        self.signals = _Held()
        #: What the main thread makes a text inside (`_Hush`).
        self.hush = _Hush(stop, self.signals.check)
        self.posts: queue.Queue = queue.Queue()
        self.inboxes: list[queue.Queue] = [queue.Queue() for _ in copies]
        #: The key of each worker's task in flight.
        self.busy: dict[int, object] = {}
        #: What was posted while the pool was halted, as (worker, key, what).
        self.left: list[tuple[int, object, object]] = []
        #: Each worker's end, set as its thread's last act. Not
        #: `Thread.is_alive`: a join a ^C cuts short can leave a thread that
        #: still runs reading as ended (3.11).
        self.gone = [threading.Event() for _ in copies]
        self.threads = [threading.Thread(target=self._run, args=(k,),
                                         name="invective-copy-%d" % k)
                        for k in range(len(copies))]
        self._ended = False
        try:
            for thread in self.threads:
                thread.start()
        except BaseException:
            # A thread that cannot be started (a limit on threads, or on
            # memory): no pool is made, so nothing would end those started
            # or put the signals' handlers back for the rest of the process.
            self.close()
            raise

    def _run(self, k: int) -> None:
        try:
            self._work(k)
        finally:
            self.gone[k].set()

    def _work(self, k: int) -> None:
        copy, inbox = self.copies[k], self.inboxes[k]
        while True:
            task = inbox.get()
            if task is None:
                return
            key, fn, args = task
            try:
                with self.hush.shared():
                    got = fn(copy, *args)
            except BaseException as exc:
                # Posted, never lost with the thread: the main thread
                # raises it, or a stop it set is what it says.
                got = exc
            self.posts.put((k, key, got))

    def give(self, k: int, key: object, fn: Callable, *args) -> None:
        """Have worker *k* call `fn(copy, *args)`; `take` gives back *key*
        with what it came to."""
        self.busy[k] = key
        self.inboxes[k].put((key, fn, args))

    def take(self) -> tuple[int, object, object]:
        """The next task done: (worker, key, its result or what it raised).

        Waited for in short spells, so that a signal held meanwhile is
        raised here, a ^C on Windows too, and so that a worker that has
        ended is noticed: one that ended with nothing posted is a `Refusal`,
        never a wait without end. The queue is read once more before that,
        for a post made just before the end.
        """
        while True:
            self.signals.check()
            try:
                k, key, got = self.posts.get(timeout=_POLL)
            except queue.Empty:
                if all(thread.is_alive() for thread in self.threads):
                    continue
                try:
                    k, key, got = self.posts.get_nowait()
                except queue.Empty:
                    raise Refusal("a worker ended without saying what its "
                                  "run came to") from None
            del self.busy[k]
            return k, key, got

    def on(self, k: int, fn: Callable, *args):
        """`fn(copy, *args)` on worker *k*, with nothing else in flight: what
        it returned, or what it raised, raised here."""
        self.give(k, k, fn, *args)
        _k, _key, got = self.take()
        if isinstance(got, BaseException):
            raise got
        return got

    def each(self, fn: Callable) -> list:
        """`fn(copy)` on every copy at once; the results in copy order."""
        for k in range(len(self.copies)):
            self.give(k, k, fn)
        got: dict = {}
        while len(got) < len(self.copies):
            _k, key, result = self.take()
            if isinstance(result, BaseException):
                raise result
            got[key] = result
        return [got[k] for k in range(len(self.copies))]

    def map(self, fn: Callable, items) -> list:
        """`fn(copy, item)` for every item, each on whichever copy is
        free; the results in item order."""
        todo = collections.deque(enumerate(items))
        idle = list(range(len(self.copies)))
        got: dict = {}
        total = len(todo)
        while len(got) < total:
            while idle and todo:
                i, item = todo.popleft()
                self.give(idle.pop(0), i, fn, item)
            k, key, result = self.take()
            idle.append(k)
            if isinstance(result, BaseException):
                raise result
            got[key] = result
        return [got[i] for i in range(total)]

    def close(self) -> None:
        """End every worker once its inbox is done."""
        if self._ended:
            return
        self._ended = True
        for inbox in self.inboxes:
            inbox.put(None)
        try:
            for thread in self.threads:
                # Every one is started but in a pool that failed to start.
                if thread.ident is not None:
                    thread.join()
        finally:
            self.signals.end()

    def halt(self) -> list[tuple[int, object, object]]:
        """Stop every run in flight, end every worker, and give back what
        was posted meanwhile (`left`).

        A signal that lands here is held (`_Held`), and raised once every
        worker has ended, as `tree._remove` lets one go on only once the
        copy is gone. One raised here all the same (^C, or the SIGTERM
        `stopping_on_sigterm` turns into one) goes on only then too, and
        the joins are in short spells, so that it does.
        """
        if self._ended:
            return self.left
        self._ended = True
        try:
            self._halt()
        finally:
            self.signals.end()
        return self.left

    def _halt(self) -> None:
        interrupted, told = None, False
        while True:
            # From the stop on, inside the loop: a ^C that lands while the
            # workers are being told is held as one that lands in a join.
            try:
                self.stop.set()
                if not told:
                    for inbox in self.inboxes:
                        inbox.put(None)
                    told = True
                    if len(self.copies) > 1:
                        self.say("stopping %d run(s)" % len(self.busy))
                for gone in self.gone:
                    gone.wait(_POLL)
                # Read before the queue is: a worker posts, then ends, so
                # once every one has ended, all they posted is there to be
                # read. Read after, a post made between the two is lost.
                ended = all(gone.is_set() for gone in self.gone)
                while True:
                    try:
                        self.left.append(self.posts.get_nowait())
                    except queue.Empty:
                        break
                if ended:
                    for thread in self.threads:
                        thread.join()
                    break
            except KeyboardInterrupt as exc:
                interrupted = exc
        if interrupted is not None:
            raise interrupted


def _measure(mutant: Mutant, copy: Copy, attempts: list[Attempt],
             budget: float) -> Outcome:
    """What *mutant* comes to in *copy*: the first outcome an attempt gives,
    in their order, or failing them all, the whole selection's.

    The whole selection is run last and is never left out, so a survivor
    is always the whole selection's verdict (`_SURVIVOR_VIA`).
    """
    for attempt in attempts:
        got = attempt(mutant, copy)
        if got is None:
            continue
        if got.verdict.ok and got.via not in _SURVIVOR_VIA:
            raise ValueError("a survivor reached by %r, which runs fewer "
                             "tests than the selection" % got.via)
        return got
    return Outcome(_final(copy.run(mutant, budget), mutant, copy.rel))


def _final(verdict: Verdict, mutant: Mutant, target: str) -> Verdict:
    """*verdict*, a run of the whole selection on *mutant*, unless it is the
    harness's and not the mutant's: then a `Refusal`."""
    if (verdict.code in (ExitCode.USAGE_ERROR, ExitCode.NO_TESTS_COLLECTED)
            and not verdict.killer):
        # The baseline proved this selection collects, and no module
        # failed to, so this is the harness and not the mutant. Scoring it
        # as a kill would be arithmetic over a run that executed no test.
        raise Refusal(
            "%s:%d %s made pytest exit %d -- it collected nothing, so "
            "this is the runner and not the mutation. Counting it as "
            "a kill would score a run that never ran a test.\n%s"
            % (target, mutant.job.line, mutant.job.what, verdict.code,
               verdict.tail))
    return verdict


def _gate(copy: Copy, path: str, timeout: float) -> tuple[Verdict, float]:
    """The tests the file *path* names, one a line, run on *copy*'s
    original, and how long that took. They can stand for the selection on
    a mutant only when the run is `ok` with nothing `missing`."""
    started = time.monotonic()
    got = copy.run(None, timeout, path)
    return got, time.monotonic() - started


def _plain_tests(tests: list[str], where: str) -> bool:
    """Whether every one of *tests* is a path in *where*, or a node id of
    one: then a run of other tests, given them to start pytest's search for
    its settings, leaves out nothing they would have chosen (an option such
    as `-k`, which names no path, would be left out with them)."""
    return all(test.partition("::")[0] and os.path.exists(
        os.path.join(where, test.partition("::")[0])) for test in tests)


class Probe(NamedTuple):
    """A mutant's last killer, which passed alone on the original: the node
    id (*killer*), the file that names it alone (*path*), and how long that
    run took (*took*)."""

    killer: str
    path: str
    took: float


def probe_attempt(probes: dict[int, Probe], budget: float) -> Attempt:
    """The attempt that runs a mutant's last killer alone, for each mutant
    whose site's index is among *probes*.

    **A kill only when the killer fails, alone, as itself**: pytest's code
    for a test that failed, the killer named the one remembered, and
    nothing missing. Under the contract a test fails alone as it fails in
    the whole selection, so that is a kill of the whole selection. Anything
    else (a pass, another test failing, a run stopped at its time, a module
    that would not import) says nothing, and the whole selection decides.
    A run cut short by the campaign stopping is `Stopped`, let through, so
    that it is never an outcome.
    """
    def attempt(mutant: Mutant, copy: Copy) -> Outcome | None:
        probe = probes.get(mutant.job.idx)
        if probe is None:
            return None
        # The killer alone takes a fraction of the selection's time, so a
        # run far past its own falls to the whole selection early.
        got = copy.run(mutant, min(budget, max(5.0, 3 * probe.took)),
                       probe.path)
        if (got.code == ExitCode.TESTS_FAILED and got.killer == probe.killer
                and not got.missing):
            return Outcome(got, via="probe")
        return None

    return attempt


#: What the header says of a test, or a few, that the campaign ran apart
#: from the rest of the selection on the original and that did not pass: the
#: whole selection passed there, so such a test is likely one that depends on
#: its order, or on another copy's run. Said, never refused: the test is only
#: left out of the speedup it was run for.
NOT_GREEN_APART = "not green on the original when run apart from the rest"


def _apart_line(names: list[str]) -> str:
    """The header's line for the runs apart from the rest, on the original,
    that were not green: how many, and the first three of *names*."""
    return ("apart:     %d %s: %s; --no-unsafe-speedups runs only the whole "
            "selection" % (len(names), NOT_GREEN_APART, ", ".join(names[:3])))


def _landed(mutant: Mutant, outcome: Outcome) -> None:
    """*outcome* stands for *mutant*: called on the main thread, once for
    each mutant whose outcome is final, and never for one that is not. Every
    outcome is final as it ends, at any count of workers, but for a kill
    made with `--confirm` and more than one worker: that lands once it is
    confirmed, after the last mutant has run, and a kill read back lands
    as it ends, having been confirmed when it was kept. A campaign that is
    stopping lands only those already final. The place for whatever keeps
    verdicts beyond the campaign."""


def mutate(root: str, target: str, tests: list[str], only: list[str] | None,
           limit: int | None, say=print, ref: str | None = None,
           exclude: tuple[str, ...] = (), selection: list[str] | None = None,
           options: tuple[str, ...] = (), workers: int | str = 1,
           confirm: bool = False, history: bool = False,
           unsafe_speedups: bool = True) -> dict:
    """Run every mutant of *target* against *tests*, and report on each.

    The mutants are written in a copy of *root* as it stands, *exclude* left
    out of it (`tree.working_tree`), or with *ref* in a worktree of that
    commit (`tree.git_ref`). *selection* is a list of node ids to run
    instead of whatever *tests* collects, *tests* then only steering
    pytest's search for its settings, and *options* go to every run's
    pytest. A campaign whose runs would not go by the project's pytest
    settings is refused (`config.check_pytest_settings`). *say* receives
    each line of progress as it happens.

    *workers* mutants are run at once, each in a copy of its own
    (`config.workers` says what it may be). The suite is taken to be one
    whose tests are independent of their order and safe to run in parallel;
    with *confirm* and more than one worker, every kill is confirmed once
    the last mutant has run, with nothing else running, and the report
    names each kill its killer did not make alone (`unreproduced`).

    With *history*, each mutant's last killer, kept in *root*'s
    `.invective/` (`store.History`), is run alone before the whole
    selection (`probe_attempt`), and each verdict is kept there for the next
    run. Without *unsafe_speedups*, neither of these runs, nor more than one
    worker (`config.settle`).
    """
    # The engine is held to the switch too, whoever called it.
    held = settle(Config(workers=workers, history=history,
                         unsafe_speedups=unsafe_speedups))
    workers, history = held.workers, held.history
    try:
        src_rel = relative_to_root(target, root)
    except ValueError:
        # Joined to the copy, this path would lead out of it, and a mutant
        # would be written over whatever file it lands on.
        raise Refusal("%s is outside the project at %s"
                      % (target, root)) from None

    # The handler lives exactly as long as the copy: here and not in the
    # commands' entry points, so `pytest --mutate`, which has none, gets it
    # too, and a plain pytest with the plugin installed never does.
    with process.stopping_on_sigterm(), \
            tempfile.TemporaryDirectory(prefix="invective-selection-") as box, \
            contextlib.ExitStack() as held_open:
        # The first copy, which the sites are read from: every other copy
        # is made from it, and so are the commit's under *ref*.
        first = held_open.enter_context(git_ref(root, ref) if ref
                                        else working_tree(root, exclude))

        def settled(where):
            # Settled before anything is said of the campaign: a run that
            # could not go by the project's settings is refused as itself.
            # A run given its settings file with `-c`, as `pytest --mutate`
            # forwards one, searches for none.
            if "-c" not in options:
                check_pytest_settings(root, where, tests, ref=bool(ref))

        def reached(where):
            # **Where the writes land, links followed.** The copy keeps
            # links as links, so a module reached through one -- the file
            # itself or a directory on its path -- can be a file outside
            # the copy, the project's own among them. A link that stays
            # inside the copy is written through, as the tests that import
            # it read through it.
            real = os.path.realpath(os.path.join(where, src_rel))
            if not real.startswith(os.path.join(os.path.realpath(where), "")):
                raise Refusal("%s reaches %s through a link, outside the copy "
                              "the mutants are written in" % (src_rel, real))

        settled(first)
        listed = None
        if selection is not None:
            # One node id a line, in a file of invective's own: a selection
            # of thousands is too long for a Windows command line.
            listed = os.path.join(box, "selection.txt")
            with open(listed, "w", encoding="utf-8") as fh:
                fh.write("".join(node + "\n" for node in selection))
        say("copy:      %s" % first)

        path = os.path.join(first, src_rel)
        if not os.path.isfile(path):
            raise Refusal("%s is not in the tree the mutants are made in" % src_rel)
        reached(first)
        # **`newline=""`, the mirror of `_write`'s.** A mutant must differ
        # from the file only at the mutation, so it is built from the file's
        # own line endings: read with the default's translation, every `\r\n`
        # of a Windows checkout would be written back as `\n`, and the mutant
        # would differ at every line. The one line rule is `_LINE_END`'s, so
        # a line of the report is a line the parser counted.
        with open(path, encoding="utf-8", newline="") as fh:
            source = fh.read()
        lines = _report_lines(source)
        module = _Module(source)
        accepts = read_accepts(source, src_rel)
        every = [(n.lineno, w) for _k, n, w in module.sites]   # type: ignore[attr-defined]
        sites = [(i, k, n, w) for i, (k, n, w) in enumerate(module.sites)
                 if not only or k in only]
        if limit:
            # Evenly spaced rather than the first N, so a cap does not mean
            # "the top of the file only".
            step = max(1, len(sites) // limit)
            sites = sites[::step][:limit]
        if not sites:
            raise Refusal("no mutation sites in %s for %s"
                          % (src_rel, ",".join(only or sorted(OPERATORS))))

        count = workers_of(workers, most=len(sites))
        # Whether a run of other tests than the selection, given *tests* to
        # start pytest's search for its settings, leaves out nothing they
        # would have chosen: on the command line, an option among the tests
        # (`-k`) would be left out with them.
        plain = selection is not None or _plain_tests(tests, first)
        # The path with `/` on every platform, as git writes a diff and as
        # the report's node ids are spelt: it is part of a text another tool
        # reads (a diff's headers, the history's keys), unlike `target`,
        # which is a path on this machine and keeps its native separators.
        shown = src_rel.replace(os.sep, "/")
        remembered = store.History.load(root, shown, say) if history else None
        # **Every copy before the first run**, so that none holds what a
        # run left behind: a cache, a database, a file a test writes.
        places = [first]
        commit = commit_of(first) if ref and count > 1 else None
        for _k in range(1, count):
            where = held_open.enter_context(
                git_ref(root, commit) if ref
                else working_tree(root, exclude, source=first))
            settled(where)
            reached(where)
            say("copy:      %s" % where)
            places.append(where)
        say("workers:   %d" % count)
        if confirm and count == 1:
            # Said, not refused: `confirm` in the settings is for the runs
            # with workers, and a run with one is a serial campaign.
            say("confirm:   nothing to confirm with one worker")
        say("project:   %s" % root)
        say("target:    %s" % src_rel)
        say("tests:     %s" % (" ".join(tests) if selection is None else
                               "%d collected by pytest" % len(selection)))
        say("speedups:  %s" % ("all" if unsafe_speedups else "no unsafe ones"))

        stop = threading.Event()
        copies = [Copy(where, src_rel, source, tests, listed, options, stop,
                       len(sites)) for where in places]
        pool = _Pool(copies, stop, say)

        def ended(kind, exc, tb):
            # Ended before the copies are removed: a run still going would
            # hold its copy.
            if kind is None:
                pool.close()
            else:
                pool.halt()

        held_open.push(ended)

        def baseline(copy: Copy, **handshake) -> tuple[float, Verdict]:
            """How long the selection took on *copy*'s original, and what it
            said: refused here unless it ran, green or red, whole."""
            started = time.time()
            got = copy.run(None, BASELINE_TIMEOUT, **handshake)
            took = time.time() - started
            if got.code == TIMED_OUT:
                raise Refusal(
                    "the selection took longer than %d s on the unmutated "
                    "tree, so there is no time to measure a mutant against"
                    % BASELINE_TIMEOUT)
            if got.code in (ExitCode.USAGE_ERROR, ExitCode.NO_TESTS_COLLECTED):
                raise Refusal(
                    "the selection collected nothing on the unmutated tree "
                    "(pytest exited %d), so there is no suite here to notice "
                    "a mutation. Every mutant would score as killed by a run "
                    "that never ran a test.\n%s" % (got.code, got.tail))
            if got.missing:
                # The selection was collected somewhere else -- a checkout
                # that has moved on, or a commit the tests are not at.
                raise Refusal(
                    "%d of the tests selected are not in the tree the mutants "
                    "are made in, among them %s"
                    # invective: accept[equivalent: 3 -> 4] how many are named
                    % (len(got.missing), ", ".join(got.missing[:3])))
            return took, got

        # **The first copy runs the selection alone, then every copy runs
        # it at once.** Alone, it proves the suite green with nothing else
        # running; at once, each copy must be green too, and the slowest
        # run is the load every mutant's runs will be under, which the
        # budget is measured from.
        # The tests the first run kept, for the history's killers to be
        # found among: asked for only when there is a history to read.
        alone, got = pool.on(0, functools.partial(baseline, inventory=True)
                             if history else baseline)
        if not got.ok:
            raise Refusal(
                "the selection is RED on the unmutated tree, so every mutant "
                "would be 'killed' for a reason that is not the mutation and "
                "the score would read perfect. Fix the suite first.\n%s"
                % got.tail)
        base = alone
        if count > 1:
            together = pool.each(baseline)
            red = [copy.where for copy, (_took, got) in zip(copies, together)
                   if not got.ok]
            if red:
                # **Green alone and red beside its own copies** is a suite
                # whose tests share something (a port, a path, a database)
                # or need their order, and every mutant's run in it would be
                # judged by which runs it met.
                raise Refusal(
                    "the selection is green alone and RED in %d of %d copies "
                    "running it at once (%s): its tests are not safe to run "
                    "in parallel, or depend on their order, and a mutant "
                    "could be 'killed' by another copy's run. Run it with "
                    "--no-unsafe-speedups, which runs one mutant at a time "
                    "against the whole selection.\n%s"
                    % (len(red), count, ", ".join(red), next(
                        got.tail for _took, got in together if not got.ok)))
            base = max(took for took, _got in together)
        say("baseline:  green in %.1fs" % base)

        # invective: accept[equivalent: 3 -> 4] any budget well above the baseline serves, and a kill by time is counted apart
        budget = max(30.0, base * 3)
        # A kill is confirmed with nothing else running, and never on less
        # time than the loaded budget gave it.
        # invective: accept[equivalent: 3 -> 4] any budget well above the baseline serves, and a kill by time is counted apart
        alone_budget = max(budget, alone * 3)

        # **Each mutant's last killer, run alone on the original first.**
        # Only a killer that passes alone there can say anything alone on
        # a mutant, and only one among the tests the selection holds, so
        # that its kill is one the selection would make. The runs not green
        # are said, a test that fails apart from the rest being likely one
        # that depends on its order or on another copy's run.
        probes: dict[int, Probe] = {}
        apart: list[str] = []
        if remembered is not None:
            keys = store.mutant_keys(lines, module.sites)
            # Kept on the way out, whatever ends the campaign: what landed
            # by then is what this run measured.
            held_open.callback(remembered.save, keys)
            known = {idx: remembered.killers[keys[idx]]
                     for idx, _kind, _node, _what in sites
                     if keys[idx] in remembered.killers}
            if plain and known:
                kept = set(got.selected if selection is None else selection)
                killers = list(dict.fromkeys(
                    killer for killer in known.values() if killer in kept))
                files = []
                for i, killer in enumerate(killers):
                    one = os.path.join(box, "probe-%d.txt" % i)
                    with open(one, "w", encoding="utf-8") as fh:
                        fh.write(killer + "\n")
                    files.append(one)
                gated = pool.map(functools.partial(_gate, timeout=budget),
                                 files)
                passed = {}
                for killer, one, (verdict, took) in zip(killers, files, gated):
                    if verdict.ok and not verdict.missing:
                        passed[killer] = Probe(killer, one, took)
                    elif not verdict.ok:
                        apart.append(killer)
                probes = {idx: passed[killer] for idx, killer in known.items()
                          if killer in passed}
                say("history:   %d remembered, %d usable"
                    % (len(known), len(probes)))
            elif not plain:
                say("history:   not used: --tests has options")
        if apart:
            say(_apart_line(apart))
        say("mutants:   %d\n" % len(sites))

        survivors, accepted, kills, killed, broken = [], [], [], 0, 0
        stale = {}
        # **Every mutant gets its own whole second, or a verdict can belong to
        # the mutant before it.** CPython invalidates a cached `.pyc` on the
        # source's `(mtime, size)` -- and it stores that mtime as WHOLE
        # SECONDS. Two mutants of the same line are routinely the same length
        # (`==` for `!=` exactly so), and two writes of this file can land
        # inside one second when the selection is quick. Identical second,
        # identical size: the interpreter reuses the PREVIOUS mutant's
        # bytecode, and the run reports on an edit it never loaded. Stamping a
        # strictly increasing mtime makes the pair differ every time, and the
        # first stamp is a second past the clock, which is never earlier than
        # the copy's own file. Only this file is stamped, so every other
        # module keeps its cache and the cost is nothing. Each mutant is the
        # whole file, so the next write replaces the last one and every run
        # differs from the copy by exactly one edit. Each copy stamps its own
        # file (`Copy`).
        clock = int(time.time())
        for copy in copies:
            copy.clock = clock
        jobs = collections.deque(
            Job(n, idx, kind, what, node.lineno)            # type: ignore[attr-defined]
            for n, (idx, kind, node, what) in enumerate(sites, 1))
        attempts: list[Attempt] = []
        if probes:
            attempts.append(probe_attempt(probes, budget))

        def made(job: Job) -> Mutant:
            try:
                # No worker warns while the warning filters are aside.
                with pool.hush.alone():
                    text, spliced = _text_of(module, job.idx)
            except Refusal as exc:
                raise Refusal("%s:%d %s %s"
                              % (src_rel, job.line, job.what, exc)) from exc
            return Mutant(job, text, spliced)

        def measured(copy: Copy, mutant: Mutant) -> Outcome:
            return _measure(mutant, copy, attempts, budget)

        def final(outcome: Outcome) -> bool:
            # Asked to confirm, a kill made while other copies ran is final
            # only once it is: a test that is not safe in parallel can fail
            # because another copy's run holds a port, a path or a database.
            return (count == 1 or not confirm or outcome.verdict.ok
                    or outcome.via == "cache")

        # The edit as a reader sees it, for every entry the mutant makes:
        # kind, line and change find a site but do not show what a `NOT`
        # or `RAISE` across several lines became, or which of two sites on
        # one line this was. Split by `_LINE_END`, as the splice and the
        # report's `line` are, so the `@@` numbers are the report's lines,
        # no `\r` is left on a line, and a `\r\n` file's diff is the
        # edited lines alone, both sides having lost their endings alike.
        # The headers spell the path with `/` (`shown`).

        def entry(mutant: Mutant, outcome: Outcome) -> dict:
            job = mutant.job
            diff = "\n".join(difflib.unified_diff(
                lines, _report_lines(mutant.text), fromfile=shown,
                tofile=shown + " (mutant)", lineterm=""))
            said = {"kind": job.kind, "line": job.line, "change": job.what,
                    "diff": diff}
            if not mutant.spliced:
                # Said beside the entry and not only in the closing lines,
                # because this is the mutant whose line numbers are not the
                # file's and whose diff is the whole file.
                said["whole_file"] = True
            for key in ("via", "origin", "confirmed"):
                if getattr(outcome, key):
                    said[key] = getattr(outcome, key)
            return said

        # Each final outcome, until its turn to be reported comes: they are
        # reported in the order of their sites, whatever order they end in.
        done: dict[int, tuple[dict, Outcome]] = {}
        reported = 0

        def report_done() -> None:
            nonlocal killed, broken, reported
            while reported + 1 in done:
                reported += 1
                mutant, outcome = done.pop(reported)
                got = outcome.verdict
                line, what = mutant["line"], mutant["change"]
                text = lines[line - 1].strip()
                covering = [a for a in accepts if a.covers(line, what)]
                if got.ok and covering:
                    accepted.append({**mutant, "source": text,
                                     "reason": covering[0].reason,
                                     "why": covering[0].why})
                    say("  accepted  %s:%d  %-28s %s" % (src_rel, line, what,
                                                        covering[0].reason))
                elif got.ok:
                    survivors.append({**mutant, "source": text})
                    # invective: accept[equivalent: 60 -> 61] a display width
                    start = text[:60]
                    say("  SURVIVED  %s:%d  %-28s %s" % (src_rel, line, what,
                                                        start))
                else:
                    killed += 1
                    # **Which check is load-bearing, not merely that one
                    # was.** A killed mutant with no killer beside it says
                    # the suite noticed; with one, it says what the line is
                    # FOR -- and a line whose only killer is a test about
                    # something else is a finding of its own.
                    kills.append({**mutant, "killer": got.killer,
                                  "code": got.code})
                    # A module that would not import, under the mutant:
                    # pytest exits 2 for that when the selection names
                    # files, and 4 -- "found no collectors" -- when it names
                    # node ids. The killer is the module that failed.
                    if got.code in (ExitCode.INTERRUPTED, ExitCode.INTERNAL_ERROR,
                                    ExitCode.USAGE_ERROR,
                                    ExitCode.NO_TESTS_COLLECTED):
                        broken += 1
                    for a in covering:
                        stale.setdefault(a, "%s is killed by %s" % (
                            what, got.killer or "a run that failed"))

        def land(mutant: Mutant, outcome: Outcome) -> None:
            # `_landed` is looked up as each lands, so a test can stand in.
            _landed(mutant, outcome)
            if remembered is not None:
                remembered.note(keys[mutant.job.idx], outcome.verdict.ok,
                                outcome.verdict.killer)

        def lands(mutant: Mutant, outcome: Outcome) -> None:
            land(mutant, outcome)
            done[mutant.job.n] = (entry(mutant, outcome), outcome)
            report_done()

        # **The pool.** Each worker is given the next mutant as soon as it
        # is free, and the main thread makes one more mutant's text for each
        # run in flight while they run, so a worker never waits for one. A
        # text that cannot be written is therefore refused while mutants
        # before it are still running, with the sentence a serial campaign
        # gives.
        running: dict[int, Mutant] = {}
        ahead: collections.deque[Mutant] = collections.deque()
        idle = list(range(count))
        unconfirmed: list[tuple[Job, Outcome]] = []
        unreproduced: list[dict] = []
        # How many have ended, and how many of those survived with no
        # acceptance: with one worker, the survivors reported so far.
        finished = survived = 0
        try:
            while jobs or ahead or running:
                while idle and (ahead or jobs):
                    mutant = ahead.popleft() if ahead else made(jobs.popleft())
                    running[mutant.job.n] = mutant
                    pool.give(idle.pop(0), mutant.job.n, measured, mutant)
                if jobs and len(ahead) < len(running) and pool.posts.empty():
                    ahead.append(made(jobs.popleft()))
                    continue
                k, n, got = pool.take()
                idle.append(k)
                if isinstance(got, BaseException):
                    raise got
                mutant = running.pop(n)
                if final(got):
                    lands(mutant, got)
                else:
                    # Its text is made again to confirm it, rather than held
                    # through the rest of the campaign.
                    unconfirmed.append((mutant.job, got))
                finished += 1
                survived += got.verdict.ok and not any(
                    a.covers(mutant.job.line, mutant.job.what)
                    for a in accepts)
                if finished % 10 == 0:
                    say("  ... %d/%d, %d survived"
                        % (finished, len(sites), survived))
        except BaseException:
            # What ended while the runs were being stopped stands if it was
            # final; a kill that was not confirmed is dropped, and so is a
            # run cut short, which is `Stopped`, never an outcome.
            for _k, n, got in pool.halt():
                if isinstance(got, Outcome) and final(got):
                    land(running[n], got)
            raise

        if unconfirmed:
            # **Asked to, every kill is confirmed with nothing else
            # running**, in the first copy with the original back in it.
            # Each kill whose killer, a test, does not make it alone is
            # named: the killer can be one that depends on its order or on
            # another copy's run, which is what the suite was taken not to
            # do. The first run is the whole selection on the original: the
            # mutants' runs there can have left something behind that a test
            # reads, and a copy no longer green on the original would score
            # a kill for that. Each run is the first worker's, one at a time.
            def restored(copy: Copy) -> Verdict:
                copy.restore()
                return copy.run(None, alone_budget)

            def again(copy: Copy, mutant: Mutant,
                      path: str | None = None) -> Verdict:
                return copy.run(mutant, alone_budget, path)

            guard = pool.on(0, restored)
            if not guard.ok or guard.missing:
                raise Refusal(
                    "the selection is not green in the first copy once the "
                    "original is back in it, after mutants ran there, so no "
                    "kill can be confirmed in it: a run left something "
                    "behind that a test reads.\n%s" % guard.tail)
            # A killer that is a test, run alone on the original, must pass
            # there, or its failing alone on the mutant says nothing. Only
            # where a run of other tests leaves out nothing the selection
            # chose: on the command line, an option among the tests would
            # be left out with them.
            usable = {}
            if plain:
                killers = dict.fromkeys(
                    outcome.verdict.killer for _job, outcome in unconfirmed
                    if "::" in outcome.verdict.killer)
                for i, killer in enumerate(killers):
                    one = os.path.join(box, "killer-%d.txt" % i)
                    with open(one, "w", encoding="utf-8") as fh:
                        fh.write(killer + "\n")
                    verdict, _took = pool.on(0, _gate, one, alone_budget)
                    if verdict.ok and not verdict.missing:
                        usable[killer] = one
            for job, outcome in sorted(unconfirmed):
                mutant = made(job)
                killer = outcome.verdict.killer
                confirmed = None
                alone_said = ""
                if killer in usable:
                    got = pool.on(0, again, mutant, usable[killer])
                    if (got.code == ExitCode.TESTS_FAILED
                            and got.killer == killer and not got.missing):
                        confirmed = outcome._replace(confirmed="alone")
                    else:
                        alone_said = "passes alone on the mutant"
                elif "::" in killer and plain:
                    alone_said = "fails alone on the original"
                if confirmed is None:
                    # The whole selection again decides it, a survivor
                    # included: this run is the only one with nothing else
                    # running.
                    confirmed = Outcome(
                        _final(pool.on(0, again, mutant), mutant, src_rel),
                        confirmed="full")
                    if not alone_said and confirmed.verdict.ok:
                        # A kill that did not come back, though no killer
                        # could say so alone: named all the same, since
                        # this is what the diagnosis is for.
                        alone_said = NO_KILLER
                if alone_said:
                    unreproduced.append({
                        "kind": job.kind, "line": job.line, "change": job.what,
                        "killer": killer, "alone": alone_said,
                        "again": ("survived" if confirmed.verdict.ok
                                  else "killed")})
                lands(mutant, confirmed)
        pool.close()

        if reported != len(sites):
            # Unreachable while every mutant ends in an outcome or a
            # refusal; a report that left one out would read as complete.
            raise Refusal("%d of the %d mutants came to no verdict"
                          % (len(sites) - reported, len(sites)))

        for a in accepts:
            if not any(a.covers(line, what) for line, what in every):
                stale.setdefault(a, "line %d has no mutant %s" % (
                    a.line, a.change if a.change else "at all"))
        stale_ = [{"line": a.at, "applies_to": a.line, "reason": a.reason,
                   "change": a.change, "why": a.why, "problem": problem}
                  for a, problem in sorted(stale.items())]
        for item in stale_:
            say("  STALE     %s:%d  accept[%s]  %s" % (
                src_rel, item["line"], item["reason"], item["problem"]))

        report = {
            "target": src_rel, "tests": tests if selection is None else selection,
            "mutants": len(sites), "killed": killed, "kills": kills,
            "survivors": survivors, "accepted": accepted, "stale": stale_,
            # Killed by a mutation the runner could not even import or
            # collect past, rather than by a test failing on it. Still a
            # kill -- the suite did notice -- but a blunter one, and a
            # campaign that cannot see the split cannot tell a
            # well-guarded module from an unimportable one.
            "broken": broken, "workers": count}
        if confirm and count > 1:
            # There whenever the kills were confirmed, empty or not, so that
            # none named reads as none found and not as none looked for.
            report["unreproduced"] = unreproduced
        return report


def summary(report: dict) -> list[str]:
    """The closing lines of a report, as a person reads them.

    The first is also what `invective sweep` reads a module's score from.
    """
    survived = len(report["survivors"]) + len(report["accepted"])
    lines = ["%d/%d killed (%.1f%%), %d survived"
             % (report["killed"], report["mutants"],
                100.0 * report["killed"] / report["mutants"], survived)]
    if report["accepted"]:
        lines.append("           %d of the survivors are accepted in the "
                     "source" % len(report["accepted"]))
    if report["stale"]:
        lines.append("           %d acceptance(s) are stale: what they claim "
                     "is not so" % len(report["stale"]))
    timeouts = sum(k["code"] == TIMED_OUT for k in report["kills"])
    if timeouts:
        # Said apart, because a kill by time is the one a slow machine can
        # give a mutant the suite would have let through.
        lines.append("           %d of the kills were runs stopped at their "
                     "time budget, not a test failing" % timeouts)
    ended = sum(k["code"] != TIMED_OUT and k["code"] not in _PYTEST_CODES
                and not k["killer"] for k in report["kills"])
    if ended:
        # Said apart for the same reason: a run a signal ended (the
        # machine out of memory, a test that kills its own process) or that
        # crashed is no test failing on the mutant, and the suite may well
        # have let it through.
        lines.append("           %d of the kills were runs a signal or a crash "
                     "ended, not a test failing" % ended)
    if report["broken"]:
        # Said out loud rather than folded into the score: these are mutants
        # the runner could not collect past, and a file whose kills are mostly
        # of this kind is not a well-tested file.
        lines.append("           %d of the kills were collection or internal "
                     "errors, not a test failing" % report["broken"])
    probed = sum(k.get("via") == "probe" for k in report["kills"])
    if probed:
        # Said apart: a test run alone that fails is a kill of the whole
        # selection only for tests independent of their order.
        lines.append("           %d of the kills were a remembered killer run "
                     "alone" % probed)
    # `.get`: there only when the kills were confirmed.
    unreproduced = report.get("unreproduced", [])
    if unreproduced:
        # Each named, since the killer is the likely culprit: a test that
        # depends on its order or on another copy's run.
        lines.append("           %d kill(s) did not come back with the killer "
                     "alone, which may depend on its order or on another "
                     "copy's run:" % len(unreproduced))
        for item in unreproduced:
            if item["alone"] != NO_KILLER:
                alone = "%s %s" % (item["killer"], item["alone"])
            elif item["killer"]:
                alone = "%s (%s)" % (NO_KILLER, item["killer"])
            else:
                alone = NO_KILLER
            lines.append("             %s:%d %s  %s; the whole selection "
                         "again: %s" % (report["target"], item["line"],
                                        item["change"], alone, item["again"]))
    # `.get`: the key is only there when the fallback was used, over the
    # three lists a report already has.
    whole = sum(e.get("whole_file", False) for e in
                report["survivors"] + report["kills"] + report["accepted"])
    if whole:
        # Said out loud: for these, and these alone, a traceback's line
        # numbers are the reformatted file's and not the source's.
        lines.append("           %d of the mutants could not be written "
                     "inside their node's span and were written as a "
                     "reformatted file" % whole)
    return lines


def gate(report: dict, config: Config) -> list[str]:
    """Why the run fails under *config*, a sentence each; none when it passes.

    Survivors are a finding to read, and fail a run only when the project's
    `[tool.invective]` says so: some are equivalent mutants, and only a
    person can say which, in an acceptance beside the line.
    """
    failures = []
    if config.fail_on_survivors and report["survivors"]:
        failures.append("%d survivor(s) that no comment accepts"
                        % len(report["survivors"]))
    if config.fail_on_survivors and report["stale"]:
        failures.append("%d stale acceptance(s)" % len(report["stale"]))
    if (config.max_accepted is not None
            and len(report["accepted"]) > config.max_accepted):
        failures.append("%d accepted survivors, more than max-accepted = %d"
                        % (len(report["accepted"]), config.max_accepted))
    return failures


def rewrite_tests(args: list[str], cwd: str, root: str) -> list[str]:
    """pytest's arguments as typed in *cwd*, each path given from *root*,
    where every run starts.

    pytest's arguments are not all paths, so one is taken for a path only
    when its text before any `::` names a file or directory that is there:
    an option, an expression or a node id of a file that is not there is
    passed as typed, and pytest says what it makes of it. A path that leads
    out of the project is given whole, so it still names the place it named.
    """
    def place(text):
        path, sep, rest = text.partition("::")
        if not path or not os.path.exists(os.path.join(cwd, path)):
            return text
        where = os.path.abspath(os.path.join(cwd, path))
        try:
            where = relative_to_root(where, root)
        except ValueError:
            pass
        return where + sep + rest

    return [place(arg) for arg in args]


def parser() -> argparse.ArgumentParser:
    """`invective run`'s command line, apart from `main` so a test can read it."""
    ap = argparse.ArgumentParser(prog="invective run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, help="the module to break")
    ap.add_argument("--tests", required=True, nargs="+",
                    help="pytest arguments, their paths relative to the "
                         "current directory")
    ap.add_argument("--only", help="comma-separated: %s" % ",".join(sorted(OPERATORS)))
    ap.add_argument("--limit", type=int, help="cap the number of mutants")
    ap.add_argument("--json", help="write the report here as well")
    ap.add_argument("--ref", help="run on this git commit, branch or tag "
                                  "instead of the files as they stand")
    ap.add_argument("--workers", metavar="N",
                    help='run N mutants at once, each in a copy of its own; '
                         '"auto" for one per CPU. [tool.invective] workers '
                         'sets the default, which is 1')
    ap.add_argument("--confirm", action="store_true",
                    help="with workers, run each kill again with nothing else "
                         "running, and name those its killer does not make "
                         "alone. [tool.invective] confirm turns it on")
    ap.add_argument("--no-unsafe-speedups", action="store_true",
                    help="use no speedup that can give a wrong verdict on a "
                         "suite whose tests depend on their order or are not "
                         "safe to run in parallel, so that each mutant is run "
                         "against the whole selection, one at a time. "
                         "[tool.invective] unsafe-speedups = false turns it "
                         "on")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = parser()
    args = ap.parse_args(argv)

    only = [k.strip().upper() for k in args.only.split(",")] if args.only else None
    if only:
        unknown = [k for k in only if k not in OPERATORS]
        if unknown:
            print("unknown operator(s): %s" % ", ".join(unknown), file=sys.stderr)
            return 2
    # **The project's top, from anywhere inside it.** Every run starts at
    # the top of the copy, so the tests' paths are rewritten to be read from
    # there; the target needs nothing, as `mutate` reads it from here.
    root = project_root()
    tests = rewrite_tests(args.tests, os.getcwd(), root)
    try:
        flags = {}
        if args.workers is not None:
            workers_of(args.workers, "--workers")
            flags["workers"] = ("--workers", args.workers)
        config = settle(load_config(root), flags, "--no-unsafe-speedups"
                        if args.no_unsafe_speedups else "")
        report = mutate(root, args.target, tests, only, args.limit,
                        ref=args.ref, exclude=config.exclude,
                        workers=config.workers,
                        confirm=args.confirm or config.confirm,
                        history=config.history,
                        unsafe_speedups=config.unsafe_speedups)
    except Refusal as exc:
        print("\nrefused: %s" % exc, file=sys.stderr)
        return 2
    except process.Terminated as exc:
        # The copy is gone and the run's group with it; now die by the
        # signal, as the process would have without the handler.
        process.exit_by(exc)

    print()
    for line in summary(report):
        print(line)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            # invective: accept[equivalent: 1 -> 2] how wide the JSON is indented
            json.dump(report, fh, indent=1)
        print("report:    %s" % args.json)
    failures = gate(report, config)
    for failure in failures:
        print("fails:     %s" % failure)
    # 2 is a run that could not be trusted; 1 is one that ran and broke the
    # project's own rules.
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
