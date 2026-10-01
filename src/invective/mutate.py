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
import copy
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
from invective.config import Config, load as load_config
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
TIMED_OUT = -1

#: How long the unmutated selection may take, so that a suite that hangs ends
#: the campaign instead of holding it for ever.
BASELINE_TIMEOUT = 600


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

    Collected off the ORIGINAL tree so a site's reported line number is the
    line somebody can go and look at. The mutation itself is applied to a deep
    copy, because `ast.unparse` rewrites the whole file and a mutant's own line
    numbers mean nothing.
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
    """A copy of *tree* with site *index* broken, and nothing else touched."""
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
    return ast.fix_missing_locations(clone)


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
    if os.path.islink(path):
        # Written through, the mutant would land in the file the link names,
        # which can be the project's own.
        os.unlink(path)
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
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
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


def run_tests(where: str, tests: list[str], timeout: float,
              selection: str | None = None, options: tuple[str, ...] = ()
              ) -> Verdict:
    """The selection, in *where*: *tests* as pytest arguments, and when
    *selection* names a file of node ids, one a line, those tests and no
    others. *options* come before invective's own.

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
        # `-p no:randomly` keeps the order, and so `-x`'s first failure, the
        # same from run to run; `-p no:` of a plugin that is not installed is
        # a no-op.
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", *options, "-x", "-rf",
             "-p", "no:randomly", *_NO_WORKERS, "-p", pytest_invective.__name__,
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
        except (OSError, ValueError, KeyError):
            # A run that ended before its session did -- pytest could not
            # start, or a mutant broke the plugin itself -- names no test.
            killer, missing = "", ()
    # Both streams: pytest says why it could not start on stderr.
    out = _ANSI.sub("", stdout + stderr)
    return Verdict(proc.returncode == 0, proc.returncode, out[-400:], killer,
                   missing)


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

    with tempfile.TemporaryDirectory(prefix="invective-selection-") as box, \
            (tree if tree is not None else working_tree(root)) as where:
        listed = None
        if selection is not None:
            # One node id a line, in a file of invective's own: a selection
            # of thousands is too long for a Windows command line.
            listed = os.path.join(box, "selection.txt")
            with open(listed, "w", encoding="utf-8") as fh:
                fh.write("".join(node + "\n" for node in selection))
        say("copy:      %s" % where)
        say("target:    %s" % src_rel)
        say("tests:     %s" % (" ".join(tests) if selection is None else
                               "%d collected by pytest" % len(selection)))

        path = os.path.join(where, src_rel)
        if not os.path.isfile(path):
            raise Refusal("%s is not in the tree the mutants are made in" % src_rel)
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        lines = source.split("\n")
        tree_ = ast.parse(source)
        accepts = read_accepts(source, src_rel)
        every = [(n.lineno, w) for _k, n, w in _sites(tree_)]   # type: ignore[attr-defined]
        sites = [(i, k, n, w) for i, (k, n, w) in enumerate(_sites(tree_))
                 if not only or k in only]
        if not sites:
            raise Refusal("no mutation sites in %s for %s"
                          % (src_rel, ",".join(only or sorted(OPERATORS))))

        def run(timeout):
            return run_tests(where, tests, timeout, listed, options)

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
                % (len(first.missing), ", ".join(first.missing[:3])))
        say("baseline:  green in %.1fs" % base)

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
            _write(path, ast.unparse(_apply(tree_, idx)), clock + n)
            got = run(budget)
            line = node.lineno                                  # type: ignore[attr-defined]
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
            mutant = {"kind": kind, "line": line, "change": what}
            if got.ok and covering:
                accepted.append({**mutant, "source": text,
                                 "reason": covering[0].reason,
                                 "why": covering[0].why})
                say("  accepted  %s:%d  %-28s %s" % (src_rel, line, what,
                                                    covering[0].reason))
            elif got.ok:
                survivors.append({**mutant, "source": text})
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="invective run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, help="the module to break")
    ap.add_argument("--tests", required=True, nargs="+",
                    help="pytest arguments, relative to the current directory, "
                         "the project's top level")
    ap.add_argument("--only", help="comma-separated: %s" % ",".join(sorted(OPERATORS)))
    ap.add_argument("--limit", type=int, help="cap the number of mutants")
    ap.add_argument("--json", help="write the report here as well")
    ap.add_argument("--ref", help="run on this git commit, branch or tag "
                                  "instead of the files as they stand")
    args = ap.parse_args(argv)

    only = [k.strip().upper() for k in args.only.split(",")] if args.only else None
    if only:
        unknown = [k for k in only if k not in OPERATORS]
        if unknown:
            print("unknown operator(s): %s" % ", ".join(unknown), file=sys.stderr)
            return 2
    root = os.getcwd()
    try:
        config = load_config(root)
        tree = (git_ref(root, args.ref) if args.ref
                else working_tree(root, config.exclude))
        report = mutate(root, args.target, args.tests, only, args.limit,
                        tree=tree)
    except Refusal as exc:
        print("\nrefused: %s" % exc, file=sys.stderr)
        return 2

    print()
    for line in summary(report):
        print(line)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
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
