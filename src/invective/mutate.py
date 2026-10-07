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
including after a refusal. `--ref` runs on a commit instead.

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
import contextlib
import copy
import difflib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

from pytest import ExitCode

import pytest_invective
from invective.accept import read as read_accepts
from invective.config import (Config, load as load_config, project_root,
                              relative_to_root)
from invective.errors import Refusal
from invective.tree import git_ref, working_tree


# --------------------------------------------------------------------------
# pytest's own exit codes are read by name (`ExitCode`), because *every
# non-zero code is not a kill*. A mutant is killed when a TEST FAILED on it. A
# run that could not be assembled -- a usage error, a selection that
# collected nothing -- is a broken harness, and scoring it as a kill is the
# same vacuous arithmetic the red-baseline refusal exists to prevent,
# arriving one layer down: the score reads high because nothing ran.

#: Not pytest's: a run this module stopped. Any value pytest cannot exit with.
TIMED_OUT = -1  # invective: accept[equivalent: 1 -> 2] any code pytest cannot exit with serves

#: How long the unmutated selection may take, so that a suite that hangs ends
#: the campaign instead of holding it for ever.
BASELINE_TIMEOUT = 600  # invective: accept[equivalent: 600 -> 601] any cap far above a suite's time serves


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


def _apply(tree: ast.AST, index: int) -> ast.AST:
    """A copy of *tree* with site *index* broken, and nothing else touched.

    The edits here and in `_mutated_node` are the same by construction, one
    made in the whole tree and one in the node alone, and must stay so:
    `_text_of`'s comparison of the two trees is what catches a drift, by
    writing every mutant as the whole file.
    """
    clone = copy.deepcopy(tree)
    # Walk the clone in the same order, so the Nth site of the clone is the Nth
    # site of the original. `ast.walk` is deterministic (a BFS over a fixed
    # child order), which is what makes this correspondence hold.
    sites = _sites(clone)
    kind, node, _what = sites[index]
    if kind == "CMP":
        node.ops[0] = _CMP_SWAP[type(node.ops[0])]()          # type: ignore[attr-defined]
    elif kind == "BOOL":
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()   # type: ignore[attr-defined]
    elif kind == "NOT":
        # Replace the `not X` node in its parent by X. Done by rewriting the
        # tree rather than by mutating the node, because a UnaryOp cannot
        # become its own operand in place.
        clone = _Unwrap(node).visit(clone)                     # type: ignore[arg-type]
    elif kind == "CONST":
        if node.value is True or node.value is False:          # type: ignore[attr-defined]
            node.value = not node.value                        # type: ignore[attr-defined]
        else:
            node.value = node.value + 1                        # type: ignore[attr-defined]
    elif kind == "RAISE":
        clone = _ToPass(node).visit(clone)                     # type: ignore[arg-type]
    # **A t-string's interpolation carries its expression's source text**
    # (3.14's `Interpolation.str`, the template's `.expression` at run
    # time), which the parser fills and `ast.unparse` writes the
    # interpolation back from. An edit inside one leaves that text naming the
    # original expression, so it is brought in step for every interpolation
    # the edited node sits in, and for no other: an untouched `{x+1}` keeps
    # its own text, as the splice keeps it. A `raise` is a statement and is
    # never inside one.
    if _INTERPOLATION is not None and kind != "RAISE":
        edited = node.operand if kind == "NOT" else node       # type: ignore[attr-defined]
        for outer in ast.walk(clone):
            if (isinstance(outer, _INTERPOLATION)
                    and any(m is edited for m in ast.walk(outer.value))):
                outer.str = ast.unparse(outer.value)
    return ast.fix_missing_locations(clone)


#: 3.14's t-string interpolation, `None` before it.
_INTERPOLATION = getattr(ast, "Interpolation", None)


def _dump(tree: ast.AST) -> str:
    """*tree* as `ast.dump` gives it, with each t-string interpolation's
    source text written as `ast.unparse` writes its expression.

    The text is the source's spelling of an expression the tree already
    holds, so two trees that differ only there are the same program up to
    its spacing: a splice of `x+1` to `x+2` parses to the text `x+2`, where
    `_apply` can only give `ast.unparse`'s `x + 2`.
    """
    if _INTERPOLATION is None:
        return ast.dump(tree)
    tree = copy.deepcopy(tree)
    for node in ast.walk(tree):
        if isinstance(node, _INTERPOLATION):
            node.str = ast.unparse(node.value)
    return ast.dump(tree)


class _Unwrap(ast.NodeTransformer):
    """`not X` becomes `X`, for one specific node."""

    def __init__(self, target: ast.AST) -> None:
        self.target = target

    def visit_UnaryOp(self, node: ast.UnaryOp):
        self.generic_visit(node)
        return node.operand if node is self.target else node


class _ToPass(ast.NodeTransformer):
    """One `raise` becomes `pass`. The refusal that stops refusing."""

    def __init__(self, target: ast.AST) -> None:
        self.target = target

    def visit_Raise(self, node: ast.Raise):
        return ast.Pass() if node is self.target else node


# --------------------------------------------------------------------------
# **A mutant is the file with one span edited, not the file unparsed.** The
# whole file through `ast.unparse` drops every comment, re-wraps every line
# and moves every line number, so a traceback in a killed run pointed at
# lines the source does not have, a diff of the mutant was the whole file,
# and a test that reads the module's own source (`inspect.getsource`, a
# check on a file's text, a line-number assertion) failed on every mutant
# for the formatting and not the mutation: a false kill of all of them.

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


def _mutated_node(tree: ast.AST, index: int) -> tuple[str, ast.AST, ast.AST]:
    """Site *index*: its kind, its node in *tree*, and a copy of that node
    alone with the kind's edit made to it.

    The edits here and in `_apply` are the same by construction and must
    stay so: `_text_of`'s comparison of the two trees is what catches a
    drift, by writing every mutant as the whole file.
    """
    kind, node, _what = _sites(tree)[index]
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
    span's own endings, not the file's first, so a file of mixed endings has
    the same count of each in the mutant as it had.

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


def _text_of(source: str, tree: ast.AST, index: int) -> tuple[str, bool]:
    """The mutant's text, and whether it is *source* edited inside the
    site's span only (True) or the whole file unparsed (False).

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
    interpreter.

    **Before 3.12 `ast.unparse` cannot write some f-strings at all**: a
    string holding a character `repr` escapes, inside an f-string's
    expression, raises `ValueError`, since that grammar has no backslash
    there.
    The node's own text is then not tried, and when the whole file cannot
    be written either, this is a `Refusal`: there is no text of this mutant
    to hand the tests.
    """
    kind, node, edited = _mutated_node(tree, index)
    whole = _apply(tree, index)
    want = _dump(whole)
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
            if (_dump(ast.parse(out)) == want
                    and len(_LINE_END.split(out)) == len(_LINE_END.split(source))):
                return out, True
        except Exception:
            pass
    try:
        return ast.unparse(whole), False
    except ValueError as exc:
        raise Refusal("cannot be written inside its node's span, and this "
                      "Python's ast.unparse cannot write the file: %s" % exc
                      ) from exc


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

#: Each run is started in a process group of its own, so that stopping it
#: stops everything it started. A test suite can start processes of its own
#: (this one starts pytest), and killing the run alone would leave those
#: running, and theirs, for as long as they like.
if os.name == "nt":
    _OWN_GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
else:
    _OWN_GROUP = {"start_new_session": True}


#: How long a stopped run's output is waited for once its group is killed.
_STOP_GRACE = 10.0


def _stop(proc: subprocess.Popen) -> None:
    """Kill *proc* and every process it started, and collect what it said."""
    if os.name == "nt":
        # invective: accept[untestable: True -> False] Windows only, and the sweep runs on Linux
        quietly = {"capture_output": True}
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], **quietly)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.communicate(timeout=_STOP_GRACE)
    except subprocess.TimeoutExpired:
        # Something the run started left its group and still holds the
        # output open: waiting for the end of it would be waiting for ever.
        proc.kill()
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()


class _Terminated(KeyboardInterrupt):
    """A SIGTERM, raised where it landed. A `KeyboardInterrupt`, so that
    every path that unwinds on a ^C -- the live run stopped with its group,
    the copy removed, pytest's own session ended as interrupted -- unwinds on
    it unchanged. *signum* is the signal, for the exit by it at the end."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@contextlib.contextmanager
def stopping_on_sigterm():
    """A SIGTERM during the body raises `_Terminated` in it, once.

    Without a handler the process dies where it stands and no `finally`
    runs, so a copy of the project stays in the temporary directory. With
    one that raises, the signal unwinds exactly as a ^C does: it interrupts
    the `communicate` that waits on a run (the syscall is retried only when
    the handler returns), the run's group is stopped on the way out, and
    the copy is removed. **Once**: a second SIGTERM while the copy is being
    removed would raise inside that removal and cut it short, so later ones
    are ignored until the previous handler is back. SIGKILL remains the way
    to stop a cleanup that hangs, and the copy's marker covers what that
    leaves. Windows never delivers SIGTERM (`TerminateProcess` ends a process
    outright), so there the handler is installed and never runs.
    """
    fired = False

    def handler(signum, frame):
        nonlocal fired
        if fired:
            return
        fired = True
        raise _Terminated(signum)

    try:
        previous = signal.signal(signal.SIGTERM, handler)
    except ValueError:
        # Not the main thread, the only one a handler can be set from: the
        # signal keeps its default, and the marker is what covers the copy.
        yield
        return
    try:
        yield
    finally:
        # `None` is a handler Python did not install (an embedding host's, a
        # C extension's), which it cannot put back: `signal.signal` refuses
        # it, and the error would replace whatever is unwinding.
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _exit_by(exc: _Terminated) -> None:
    """End this process by the signal *exc* carries, as it would have ended
    without a handler. A shell, `timeout(1)` or CI's cancel then sees the
    status it expects of a process it terminated (143 in a shell), and not an
    exit code that reads as a verdict."""
    signal.signal(exc.signum, signal.SIG_DFL)
    os.kill(os.getpid(), exc.signum)


def run_tests(where: str, tests: list[str], timeout: float,
              selection: str | None = None, options: tuple[str, ...] = (),
              target: str = "") -> Verdict:
    """The selection, in *where*: *tests* as pytest arguments, and when
    *selection* names a file of node ids, one a line, those tests and no
    others. *options* come after `-p pytest_invective` and before the rest
    of invective's own: the plugin notes which modules were loaded before
    it, and a forwarded `-p` plugin that imports the target has to come
    after that note for the file it loaded to be the plugin's to name.
    *target*, the mutated module's path from the top of *where*, is what the
    plugin checks the tests loaded from the copy; none, and it checks
    nothing.

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
        if target:
            env[pytest_invective.TARGET] = target
        # `-p no:randomly` keeps the order, and so `-x`'s first failure, the
        # same from run to run; `-p no:` of a plugin that is not installed is
        # a no-op. The `no:` exclusions and `-n 0` are order-free: pytest
        # applies a `no:` before it imports anything.
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-p", pytest_invective.__name__,
             *options, "-x", "-rf", "-p", "no:randomly", *_NO_WORKERS,
             "--no-header", *tests],
            cwd=where, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, **_OWN_GROUP)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop(proc)
            # A mutant that hangs is a mutant the suite noticed, in the least
            # helpful way available. Counted as killed and said out loud,
            # because a timeout that is silently a pass would flatter the
            # score.
            return Verdict(False, TIMED_OUT, "TIMEOUT", "TIMEOUT")
        except BaseException:
            # Interrupted: the run is in a group of its own, so the ^C that
            # stopped this process never reached it.
            _stop(proc)
            raise
        try:
            with open(verdict, encoding="utf-8") as fh:
                said = json.load(fh)
            killer, missing = said["killer"], tuple(said["missing"])
            # `.get`: a verdict an older plugin wrote has no such field.
            elsewhere = said.get("elsewhere", "")
        except (OSError, ValueError, KeyError):
            # A run that ended before its session did -- pytest could not
            # start, or a mutant broke the plugin itself -- names no test.
            killer, missing, elsewhere = "", (), ""
    # Both streams: pytest says why it could not start on stderr.
    out = _ANSI.sub("", stdout + stderr)
    # invective: accept[equivalent: 400 -> 401] any length that holds pytest's last words serves
    return Verdict(proc.returncode == 0, proc.returncode, out[-400:], killer,
                   missing, elsewhere)


def mutate(root: str, target: str, tests: list[str], only: list[str] | None,
           limit: int | None, say=print, tree=None,
           selection: list[str] | None = None,
           options: tuple[str, ...] = ()) -> dict:
    """Run every mutant of *target* against *tests*, and report on each.

    The mutants are written in *tree*, a context manager giving a directory
    that stands for *root* (`tree.working_tree` or `tree.git_ref`); by
    default a copy of *root* as it stands. *selection* is a list of node ids
    to run instead of whatever *tests* collects, and *options* go to every
    run's pytest. *say* receives each line of progress as it happens.
    """
    try:
        src_rel = os.path.relpath(os.path.abspath(target), root)
    except ValueError:
        # Windows: the target is on another drive than the project.
        src_rel = os.pardir
    if src_rel == os.pardir or src_rel.startswith(os.pardir + os.sep):
        # Joined to the copy, this path leads out of it, and a mutant would
        # be written over whatever file it lands on.
        raise Refusal("%s is outside the project at %s" % (target, root))

    # The handler lives exactly as long as the copy: here and not in the
    # commands' entry points, so `pytest --mutate`, which has none, gets it
    # too, and a plain pytest with the plugin installed never does.
    with stopping_on_sigterm(), \
            tempfile.TemporaryDirectory(prefix="invective-selection-") as box, \
            (tree if tree is not None else working_tree(root)) as where:
        listed = None
        if selection is not None:
            # One node id a line, in a file of invective's own: a selection
            # of thousands is too long for a Windows command line.
            listed = os.path.join(box, "selection.txt")
            with open(listed, "w", encoding="utf-8") as fh:
                fh.write("".join(node + "\n" for node in selection))
        say("copy:      %s" % where)
        say("project:   %s" % root)
        say("target:    %s" % src_rel)
        say("tests:     %s" % (" ".join(tests) if selection is None else
                               "%d collected by pytest" % len(selection)))

        path = os.path.join(where, src_rel)
        if not os.path.isfile(path):
            raise Refusal("%s is not in the tree the mutants are made in" % src_rel)
        # **Where the writes land, links followed.** The copy keeps links as
        # links, so a module reached through one -- the file itself or a
        # directory on its path -- can be a file outside the copy, the
        # project's own among them. A link that stays inside the copy is
        # written through, as the tests that import it read through it.
        real = os.path.realpath(path)
        if not real.startswith(os.path.join(os.path.realpath(where), "")):
            raise Refusal("%s reaches %s through a link, outside the copy the "
                          "mutants are written in" % (src_rel, real))
        # **`newline=""`, the mirror of `_write`'s.** A mutant must differ
        # from the file only at the mutation, so it is built from the file's
        # own line endings: read with the default's translation, every `\r\n`
        # of a Windows checkout would be written back as `\n`, and the mutant
        # would differ at every line. The one line rule is `_LINE_END`'s, so
        # a line of the report is a line the parser counted.
        with open(path, encoding="utf-8", newline="") as fh:
            source = fh.read()
        lines = _report_lines(source)
        tree_ = ast.parse(source)
        accepts = read_accepts(source, src_rel)
        every = [(n.lineno, w) for _k, n, w in _sites(tree_)]   # type: ignore[attr-defined]
        sites = [(i, k, n, w) for i, (k, n, w) in enumerate(_sites(tree_))
                 if not only or k in only]
        if not sites:
            raise Refusal("no mutation sites in %s for %s"
                          % (src_rel, ",".join(only or sorted(OPERATORS))))

        def run(timeout):
            got = run_tests(where, tests, timeout, listed, options,
                            src_rel.replace(os.sep, "/"))
            if got.elsewhere:
                # **The tests did not load the copy's file, so no mutant
                # written there can reach them**, and every one would
                # survive, silently. An editable install's `.pth` line, or a
                # `sys.path` entry, puts the project's own `src` on the path;
                # the copy's is there only when something puts it there,
                # since `python -m pytest` adds the directory it started in,
                # the copy's top, and that does not reach a `src/`. The
                # baseline is where this fires; every run is checked because
                # the check is one field and this is the one place it is read.
                raise Refusal(
                    "%s was imported from %s, not from the copy at %s, so no "
                    "mutant of it can reach the tests. An editable install, "
                    "or a sys.path entry, points the tests at the project "
                    "itself; a conftest.py that puts the copy's own "
                    "directory for it first on sys.path (from its own "
                    "__file__) makes them import the copy."
                    % (src_rel, got.elsewhere, where))
            return got

        started = time.time()
        first = run(BASELINE_TIMEOUT)
        base = time.time() - started
        if first.code == TIMED_OUT:
            raise Refusal(
                "the selection took longer than %d s on the unmutated tree, "
                "so there is no time to measure a mutant against"
                % BASELINE_TIMEOUT)
        if first.code in (ExitCode.USAGE_ERROR, ExitCode.NO_TESTS_COLLECTED):
            raise Refusal(
                "the selection collected nothing on the unmutated tree "
                "(pytest exited %d), so there is no suite here to notice a "
                "mutation. Every mutant would score as killed by a run that "
                "never ran a test.\n%s" % (first.code, first.tail))
        if not first.ok:
            raise Refusal(
                "the selection is RED on the unmutated tree, so every mutant "
                "would be 'killed' for a reason that is not the mutation and "
                "the score would read perfect. Fix the suite first.\n%s"
                % first.tail)
        if first.missing:
            # The selection was collected somewhere else -- a checkout that
            # has moved on, or a commit the tests are not at.
            raise Refusal(
                "%d of the tests selected are not in the tree the mutants are "
                "made in, among them %s"
                # invective: accept[equivalent: 3 -> 4] how many are named
                % (len(first.missing), ", ".join(first.missing[:3])))
        say("baseline:  green in %.1fs" % base)

        # invective: accept[equivalent: 3 -> 4] any budget well above the baseline serves, and a kill by time is counted apart
        budget = max(30.0, base * 3)
        if limit:
            # Evenly spaced rather than the first N, so a cap does not mean
            # "the top of the file only".
            step = max(1, len(sites) // limit)
            sites = sites[::step][:limit]
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
        # differs from the copy by exactly one edit.
        clock = int(time.time())
        for n, (idx, kind, node, what) in enumerate(sites, 1):
            line = node.lineno                                  # type: ignore[attr-defined]
            try:
                written, spliced = _text_of(source, tree_, idx)
            except Refusal as exc:
                raise Refusal("%s:%d %s %s" % (src_rel, line, what, exc)) from exc
            _write(path, written, clock + n)
            got = run(budget)
            text = lines[line - 1].strip()
            if (got.code in (ExitCode.USAGE_ERROR, ExitCode.NO_TESTS_COLLECTED)
                    and not got.killer):
                # The baseline proved this selection collects, and no module
                # failed to, so this is the harness and not the mutant.
                # Scoring it as a kill would be arithmetic over a run that
                # executed no test.
                raise Refusal(
                    "%s:%d %s made pytest exit %d -- it collected nothing, so "
                    "this is the runner and not the mutation. Counting it as "
                    "a kill would score a run that never ran a test.\n%s"
                    % (src_rel, line, what, got.code, got.tail))
            covering = [a for a in accepts if a.covers(line, what)]
            # The edit as a reader sees it, for every entry the mutant makes:
            # kind, line and change find a site but do not show what a `NOT`
            # or `RAISE` across several lines became, or which of two sites on
            # one line this was. Split by `_LINE_END`, as the splice and the
            # report's `line` are, so the `@@` numbers are the report's lines,
            # no `\r` is left on a line, and a `\r\n` file's diff is the
            # edited lines alone, both sides having lost their endings alike.
            diff = "\n".join(difflib.unified_diff(
                lines, _report_lines(written), fromfile=src_rel,
                tofile=src_rel + " (mutant)", lineterm=""))
            mutant = {"kind": kind, "line": line, "change": what, "diff": diff}
            if not spliced:
                # Said beside the entry and not only in the closing lines,
                # because this is the mutant whose line numbers are not the
                # file's and whose diff is the whole file.
                mutant["whole_file"] = True
            if got.ok and covering:
                accepted.append({**mutant, "source": text,
                                 "reason": covering[0].reason,
                                 "why": covering[0].why})
                say("  accepted  %s:%d  %-28s %s" % (src_rel, line, what,
                                                    covering[0].reason))
            elif got.ok:
                survivors.append({**mutant, "source": text})
                # invective: accept[equivalent: 60 -> 61] a display width
                say("  SURVIVED  %s:%d  %-28s %s" % (src_rel, line, what, text[:60]))
            else:
                killed += 1
                # **Which check is load-bearing, not merely that one was.** A
                # killed mutant with no killer beside it says the suite
                # noticed; with one, it says what the line is FOR -- and a
                # line whose only killer is a test about something else is a
                # finding of its own.
                kills.append({**mutant, "killer": got.killer, "code": got.code})
                # A module that would not import, under the mutant: pytest
                # exits 2 for that when the selection names files, and 4 --
                # "found no collectors" -- when it names node ids. The killer
                # is the module that failed.
                if got.code in (ExitCode.INTERRUPTED, ExitCode.INTERNAL_ERROR,
                                ExitCode.USAGE_ERROR, ExitCode.NO_TESTS_COLLECTED):
                    broken += 1
                for a in covering:
                    stale.setdefault(a, "%s is killed by %s"
                                     % (what, got.killer or "a run that failed"))
            if n % 10 == 0:
                say("  ... %d/%d, %d survived" % (n, len(sites), len(survivors)))

        for a in accepts:
            if not any(a.covers(line, what) for line, what in every):
                stale.setdefault(a, "line %d has no mutant %s" % (
                    a.line, a.change if a.change else "at all"))
        stale_ = [{"line": a.at, "applies_to": a.line, "reason": a.reason,
                   "change": a.change, "why": a.why, "problem": problem}
                  for a, problem in sorted(stale.items())]
        for entry in stale_:
            say("  STALE     %s:%d  accept[%s]  %s" % (
                src_rel, entry["line"], entry["reason"], entry["problem"]))

        return {"target": src_rel, "tests": tests if selection is None else selection,
                "mutants": len(sites), "killed": killed, "kills": kills,
                "survivors": survivors, "accepted": accepted, "stale": stale_,
                # Killed by a mutation the runner could not even import or
                # collect past, rather than by a test failing on it. Still a
                # kill -- the suite did notice -- but a blunter one, and a
                # campaign that cannot see the split cannot tell a
                # well-guarded module from an unimportable one.
                "broken": broken}


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
    if report["broken"]:
        # Said out loud rather than folded into the score: these are mutants
        # the runner could not collect past, and a file whose kills are mostly
        # of this kind is not a well-tested file.
        lines.append("           %d of the kills were collection or internal "
                     "errors, not a test failing" % report["broken"])
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


#: The options whose value is an expression or a plugin's import name, never
#: a path, though a word of it may be spelt like an entry of the directory:
#: `-k tests` typed beside a `tests/` is still the expression.
_NOT_PATHS = frozenset({"-k", "-m", "-p"})


def rewrite_tests(args: list[str], cwd: str, root: str) -> list[str]:
    """pytest's arguments as typed in *cwd*, each path given from *root*,
    where every run starts.

    pytest's arguments are not all paths, so one is taken for a path only
    when its text before any `::` names a file or directory that is there:
    an option, an expression or a node id of a file that is not there is
    passed as typed, and pytest says what it makes of it. The value of an
    `--option=value` is read the same way, as `--ignore=tests` typed from a
    subdirectory names a place there. A path that leads out of the project
    is given whole, so it still names the place it named.

    From the command line only paths and node ids reach here today: argparse
    takes a `-k` or an `--ignore=` among `--tests` for an option of `run`'s
    own and refuses it. The options are read here all the same, so that the
    day they get through, none of them has its meaning changed.
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

    out = []
    for i, arg in enumerate(args):
        if i and args[i - 1] in _NOT_PATHS:
            out.append(arg)
        elif arg.startswith("--") and "=" in arg:
            option, _eq, value = arg.partition("=")
            out.append(option + "=" + place(value))
        else:
            out.append(place(arg))
    return out


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
        config = load_config(root)
        tree = (git_ref(root, args.ref) if args.ref
                else working_tree(root, config.exclude))
        report = mutate(root, args.target, tests, only, args.limit,
                        tree=tree)
    except Refusal as exc:
        print("\nrefused: %s" % exc, file=sys.stderr)
        return 2
    except _Terminated as exc:
        # The copy is gone and the run's group with it; now die by the
        # signal, as the process would have without the handler.
        _exit_by(exc)

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
