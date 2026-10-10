#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Compare invective at two git refs on the benchmark's cases.

    uv run bench/compare.py BEFORE AFTER --case R40 [--case M60 ...]
        [--workers 1,4] [--pairs 3] [--quick] [--out DIR]

Each side gets a temporary git worktree of its ref and a uv venv with that
invective installed, and each case's project a copy per side. The pairs run in
ABBA order, with a calibration (a plain serial pytest of one fixed file, in
the before venv) just before and just after every run. A pair whose
calibrations spread past the session's threshold is discarded and repeated,
up to a cap. Every run's verdicts must equal the first run of its case, on
either side and at any count of workers; a difference is printed as it is
found and makes the exit status 3. The out directory gets every run as a TSV
row (`runs.tsv`), the pairs (`pairs.tsv`), the calibrations
(`calibrations.tsv`), the summary (`summary.txt`) and a `**Finding:**` block
ready to paste (`finding.md`), with each run's log, report and instrument
rows under `logs/`.

Exit status: 0, every case concluded with equal verdicts; 1, a run died or a
case is inconclusive; 2, the setup failed or the arguments are wrong; 3, the
verdicts differ; 4, a pytest process count asked for by `--same-processes` or
`--expect-rerun-processes` was not met; 5, the `--confirm-run` named a kill it
could not reproduce, or could not say; 130, stopped by ^C or SIGTERM. The worktrees, venvs and
copies are removed in every case unless `--keep` is given.

The tool needs git and uv, and nothing from this project's environment: it is
the standard library only, and runs under any Python from 3.11.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
from collections.abc import Callable, Iterable, Sequence

try:
    import resource
except ImportError:  # Windows: wall time only.
    resource = None

HERE = os.path.dirname(os.path.abspath(__file__))
INSTRUMENT = os.path.join(HERE, "instrument.py")
CASES = os.path.join(HERE, "cases.toml")

ADVICE = ("advice: set the power profile to performance, keep the machine on AC "
          "power, and close other heavy work; drift shows up as discarded pairs.")

SIDES = ("before", "after")

#: A pair is discarded when its largest calibration is more than this times
#: its smallest: ten times the 1.4% three plain runs of `test_recipes.py`
#: spread on a quiet machine, and far below the 5x drift the rule catches.
THRESHOLD = 1.15

#: When a calibration of the session's first pair spreads more than this on
#: its own, the threshold becomes 1 + three times that spread: a machine
#: that noisy would discard every pair at 1.15x.
FIRST_PAIR_SPREAD = 0.05

#: The most that relaxation goes to: a first calibration caught in the very
#: drift the rule exists for (8.5, 8.6, then 30 s) would otherwise set a
#: threshold of 8.6x and discard nothing all session. A machine that needs
#: more is too noisy to measure on, and its pairs are discarded.
THRESHOLD_MOST = 1.5

#: The least noise floor taken from fewer than `NEIGHBOURS_LEAST` neighbours,
#: or from none: R40's same-engine pairs on a quiet VM ran 0.99x to 1.06x, so
#: two percent is the least the data allows.
FLOOR_LEAST = 0.02
NEIGHBOURS_LEAST = 3

#: `calibrate_runs = "auto"` takes one run when it is longer than this, else
#: the median of three.
AUTO_ONE_RUN = 20.0

#: How long a run is given to end by itself after a ^C (which reached it
#: too, from the terminal) or a SIGTERM (which is passed on to it), before it
#: is terminated, then killed: invective removes its copies on the way out.
GRACE = 30.0

#: The classes of runs `--fixed` can name as ones a change cannot touch.
FIXED_CLASSES = ("baseline", "timeout", "survivor")

#: invective's exit for a run that could not be trusted (a refusal), and
#: the two for one that ran (1: it broke the project's own rules).
REFUSED = 2
RAN = (0, 1)

#: pytest's own exit codes; any other on a kill is a signal or a crash.
PYTEST_CODES = range(0, 6)
TIMED_OUT = -1


class BenchError(Exception):
    """The setup or a calibration failed: the session cannot go on."""


class Terminated(KeyboardInterrupt):
    """A SIGTERM, unwound as a ^C is, so the temporaries are removed."""


# --------------------------------------------------------------------------
# Cases


@dataclasses.dataclass(frozen=True)
class Project:
    name: str
    repo: str | None = None
    commit: str | None = None
    path: str | None = None
    self_: bool = False
    calibrate: tuple[str, ...] | str = ()
    calibrate_runs: int | str = 3
    deselect: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    pyproject: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclasses.dataclass(frozen=True)
class Case:
    name: str
    project: str
    target: str
    tests: tuple[str, ...] = ()
    collect: tuple[str, ...] = ()
    collect_exclude: tuple[str, ...] = ()
    only: str | None = None
    limit: int | None = None
    via: str = "run"
    options: tuple[str, ...] = ()

    def command(self, tests: Sequence[str] | None = None) -> str:
        """The case as a person would type it, without the report's path or
        the count of workers."""
        tests = list(self.tests if tests is None else tests)
        if self.via == "pytest":
            argv = ["pytest", *self.options, "--mutate", self.target]
            if self.only:
                argv += ["--mutate-only", self.only]
            if self.limit is not None:
                argv += ["--mutate-limit", str(self.limit)]
            return shlex.join(argv + tests)
        argv = ["invective", "run", "--target", self.target, "--tests", *tests]
        if self.only:
            argv += ["--only", self.only]
        if self.limit is not None:
            argv += ["--limit", str(self.limit)]
        return shlex.join(argv)


_PROJECT_KEYS = {"repo", "commit", "path", "self", "calibrate", "calibrate_runs",
                 "deselect", "requires", "pyproject"}
_CASE_KEYS = {"project", "target", "tests", "collect", "collect_exclude", "only",
              "limit", "via", "options"}


def load_cases(path: str) -> tuple[dict[str, Project], dict[str, Case]]:
    """The projects and cases of the TOML file at *path*, every key checked:
    an unknown one is an error, so that a misspelt key is not read as
    absent."""
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    projects, cases = {}, {}
    for name, table in data.get("projects", {}).items():
        unknown = set(table) - _PROJECT_KEYS
        if unknown:
            raise BenchError("%s: project %s has no key %s"
                             % (path, name, ", ".join(sorted(unknown))))
        sources = [bool(table.get("repo")), bool(table.get("path")),
                   bool(table.get("self"))]
        if sum(sources) != 1:
            raise BenchError("%s: project %s needs exactly one of repo (with "
                             "commit), path, or self = true" % (path, name))
        commit = table.get("commit")
        if table.get("repo") and not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
            raise BenchError("%s: project %s's commit must be a full sha, since "
                             "a short one cannot be fetched" % (path, name))
        runs = table.get("calibrate_runs", 3)
        if runs != "auto" and not (isinstance(runs, int) and runs >= 1):
            raise BenchError("%s: project %s's calibrate_runs must be a whole "
                             "number from 1, or \"auto\"" % (path, name))
        calibrate = table.get("calibrate", ())
        if calibrate != "tests":
            calibrate = tuple(calibrate)
        local = table.get("path")
        if local and not os.path.isabs(local):
            local = os.path.join(os.path.dirname(os.path.abspath(path)), local)
        projects[name] = Project(
            name, table.get("repo"), commit, local, bool(table.get("self")),
            calibrate, runs, tuple(table.get("deselect", ())),
            tuple(table.get("requires", ())),
            tuple((t, tuple(lines)) for t, lines in table.get("pyproject", {}).items()))
    for name, table in data.get("cases", {}).items():
        unknown = set(table) - _CASE_KEYS
        if unknown:
            raise BenchError("%s: case %s has no key %s"
                             % (path, name, ", ".join(sorted(unknown))))
        if table.get("project") not in projects:
            raise BenchError("%s: case %s names no project of the file" % (path, name))
        if "target" not in table or not (table.get("tests") or table.get("collect")):
            raise BenchError("%s: case %s needs a target, and tests or collect"
                             % (path, name))
        if table.get("via", "run") not in ("run", "pytest"):
            raise BenchError("%s: case %s's via is run or pytest" % (path, name))
        cases[name] = Case(
            name, table["project"], table["target"], tuple(table.get("tests", ())),
            tuple(table.get("collect", ())), tuple(table.get("collect_exclude", ())),
            table.get("only"), table.get("limit"), table.get("via", "run"),
            tuple(table.get("options", ())))
    return projects, cases


# --------------------------------------------------------------------------
# The schedule, the discard and its repeats


def order(k: int) -> tuple[str, str]:
    """The sides of a block's pair *k* (from 0) in the order they run: B A,
    then A B, then B A, so that a drift in one direction favours neither."""
    return ("before", "after") if k % 2 == 0 else ("after", "before")


def run_block(pairs: int, repeats: int,
              run_pair: Callable[[int, tuple[str, str]], str]) -> list[str]:
    """Run pairs until *pairs* are kept, each by ``run_pair(k, order(k))``,
    which says what the pair came to: "kept", "discarded", "died" or
    "refused". A discarded or died pair is repeated, at most *repeats* times
    in the block; a refused one ends the block, since a refusal comes again.
    The statuses, in order."""
    statuses: list[str] = []
    kept = repeated = 0
    k = 0
    while kept < pairs:
        status = run_pair(k, order(k))
        statuses.append(status)
        k += 1
        if status == "kept":
            kept += 1
        elif status == "refused" or repeated >= repeats:
            break
        else:
            repeated += 1
    return statuses


def spread(values: Iterable[float]) -> float:
    """The largest of *values* over the smallest."""
    values = list(values)
    return max(values) / min(values)


def session_threshold(first_pair: Sequence[Sequence[float]]) -> tuple[float, str]:
    """The discard threshold for a session whose first pair's calibrations
    took the runs *first_pair* (one sequence of seconds per calibration),
    and why: `THRESHOLD`, unless a calibration spread more than
    `FIRST_PAIR_SPREAD` on its own, when it is 1 + three times the largest
    such spread, at most `THRESHOLD_MOST`."""
    own = max((spread(runs) - 1 for runs in first_pair), default=0.0)
    if own > FIRST_PAIR_SPREAD:
        if 1 + 3 * own > THRESHOLD_MOST:
            return THRESHOLD_MOST, (
                "%.2fx, the most it is relaxed to: a calibration of the first pair "
                "spread %.1f%% on its own, and the machine is too noisy to measure "
                "on" % (THRESHOLD_MOST, 100 * own))
        return 1 + 3 * own, ("%.2fx: a calibration of the first pair spread %.1f%% "
                             "on its own" % (1 + 3 * own, 100 * own))
    return THRESHOLD, "%.2fx" % THRESHOLD


def discarded(calibrations: Sequence[float], threshold: float) -> bool:
    """Whether the pair whose runs had the calibration medians
    *calibrations* around them drifted: the largest over the smallest is
    more than *threshold*."""
    return spread(calibrations) > threshold


# --------------------------------------------------------------------------
# The noise floor


def neighbours(runs: Sequence[tuple[str, float, bool]]) -> list[float]:
    """The ratios of each same-side neighbour in a block's runs, given in the
    order they ran as (side, wall, usable): the later's wall over the
    earlier's, for two adjacent runs of one side when both are usable. ABBA
    makes one at every boundary between pairs."""
    found = []
    for (side, wall, usable), (side2, wall2, usable2) in zip(runs, runs[1:]):
        if side == side2 and usable and usable2:
            found.append(wall2 / wall)
    return found


@dataclasses.dataclass(frozen=True)
class Floor:
    f: float
    count: int
    how: str


def floors(ratios: dict[tuple[str, int], list[float]]) -> dict[tuple[str, int], Floor]:
    """The noise floor `f` for each (project, workers): the mean of
    ``|r - 1|`` over its neighbours' ratios, every case on the project
    pooled; at least `FLOOR_LEAST` with fewer than `NEIGHBOURS_LEAST` of
    them; and with none, the largest floor measured in the session, at
    least `FLOOR_LEAST`."""
    out = {}
    for key, found in ratios.items():
        if not found:
            continue
        f = statistics.fmean(abs(r - 1) for r in found)
        if len(found) < NEIGHBOURS_LEAST and f < FLOOR_LEAST:
            out[key] = Floor(FLOOR_LEAST, len(found), "%.1f%% raised to the least"
                             % (100 * f))
        else:
            out[key] = Floor(f, len(found), "measured")
    largest = max((fl.f for fl in out.values()), default=0.0)
    for key, found in ratios.items():
        if not found:
            out[key] = Floor(max(largest, FLOOR_LEAST), 0,
                             "no neighbours: the session's largest, or the least")
    return out


# --------------------------------------------------------------------------
# The ratios


def mutant_mark(diff: str | None) -> str | None:
    """What tells apart two mutants of one kind and change on one line (two
    `and`s, two `1`s): a hash of the lines its entry's diff takes out and
    puts in, which differ by the column edited. The two header lines are
    left out, and so are the hunk headers and the context. None without a
    diff. `bench/instrument.py` makes the same mark from the text a run
    wrote, so its rows and the report's entries match one to one."""
    if diff is None:
        return None
    changed = [line for line in diff.split("\n")[2:] if line[:1] in ("+", "-")]
    return hashlib.sha1("\n".join(changed).encode("utf-8")).hexdigest()[:16]


def mutant_key(target: str, entry: dict) -> tuple:
    """A mutant's identity in a report: its target, kind, line, change and
    mark (`mutant_mark`). Kind, line and change alone name two mutants at
    once wherever a line holds the same edit twice."""
    return (target, entry["kind"], entry["line"], entry["change"],
            mutant_mark(entry.get("diff")))


def fixed_keys(reports: Sequence[dict], classes: Iterable[str]) -> set[tuple]:
    """The mutants of the before side's *reports* whose runs a change cannot
    touch, by *classes*: "timeout", the kills by time; "survivor", the
    survivors and the accepted."""
    classes = set(classes)
    keys = set()
    for report in reports:
        target = report["target"]
        if "timeout" in classes:
            keys |= {mutant_key(target, k)
                     for k in report["kills"] if k.get("code") == TIMED_OUT}
        if "survivor" in classes:
            keys |= {mutant_key(target, e)
                     for e in report["survivors"] + report["accepted"]}
    return keys


def is_baseline(row: dict) -> bool:
    """Whether the instrument's *row* is a baseline: a run of the original on
    the campaign's own selection. A gate or a probe runs a file of its own,
    and a coverage run carries the coverage handshake; neither is one, since
    each is a run a feature adds."""
    return row.get("n") is None and not row.get("prefix") and not row.get("coverage")


def fixed_seconds(calls: Sequence[dict], keys: set[tuple],
                  classes: Iterable[str]) -> float:
    """The seconds of the instrument's *calls* that a change cannot touch:
    the runs of the mutants of *keys*, and, with "baseline" in *classes*,
    each run of the original on the campaign's own selection (a gate or a
    probe, which runs a file of its own, stays touchable)."""
    baseline = "baseline" in set(classes)
    total = 0.0
    for row in calls:
        if row.get("n") is None:
            if baseline and is_baseline(row):
                total += row["seconds"]
        elif (row.get("target"), row["kind"], row["line"], row["change"],
              row.get("mark")) in keys:
            total += row["seconds"]
    return total


@dataclasses.dataclass(frozen=True)
class Ratios:
    whole: float
    touchable: float | None
    ceiling: float | None


def ratios(before: float, after: float, before_fixed: float | None,
           after_fixed: float | None) -> Ratios:
    """A pair's speedups, before over after: of the whole run, and of the
    touchable part (each wall less the seconds a change cannot touch), with
    the ceiling, the whole-run speedup if the touchable part cost nothing.
    Without the fixed seconds, or with a part that is not above zero, the
    touchable figures are None."""
    whole = before / after
    if before_fixed is None or after_fixed is None:
        return Ratios(whole, None, None)
    tb, ta = before - before_fixed, after - after_fixed
    touchable = tb / ta if tb > 0 and ta > 0 else None
    ceiling = before / before_fixed if before_fixed > 0 else None
    return Ratios(whole, touchable, ceiling)


def touchable_floor(f: float, whole: float | None, part: float | None) -> float | None:
    """The touchable speedup the noise floor *f* allows for: a whole run of
    *whole* seconds, of which *part* are touchable, moves by `2f` of
    `whole` when the touchable part speeds up `1 / (1 - 2f * whole / part)`.
    None when that is not reached by any speedup (`2f * whole` is all of
    the touchable part or more), or without the numbers."""
    if whole is None or not part or part <= 0:
        return None
    x = 2 * f * whole / part
    return None if x >= 1 else 1 / (1 - x)


def touchable_beyond(touchable: float | None, f: float, whole: float | None,
                     part: float | None) -> bool | None:
    """Whether a *touchable* speedup is beyond its own floor: above
    `touchable_floor`, or below `1 / (1 + 2f * whole / part)`, the slowdown
    of the touchable part that moves the whole run by `2f`. None without the
    numbers."""
    if touchable is None or whole is None or not part or part <= 0:
        return None
    x = 2 * f * whole / part
    needs = touchable_floor(f, whole, part)
    return (needs is not None and touchable > needs) or touchable < 1 / (1 + x)


# --------------------------------------------------------------------------
# The pytest processes


def process_count(calls: Sequence[dict] | None) -> int | None:
    """How many pytest processes a run started: one for each `Copy.run`
    the instrument recorded, or None without the instrument."""
    return None if calls is None else len(calls)


def rerun_expected(calls: Sequence[dict] | None, reports: Sequence[dict]) -> int | None:
    """How many pytest processes a re-run that remembers every verdict it
    can starts, from the run before it (*calls*, *reports*): its baselines
    again (`is_baseline`), and one run for each mutant it killed by load (by
    time, or a run a signal or a crash ended), since no such kill is ever
    remembered. None without the instrument.

    A run another feature adds is counted by adding its own term here: a
    coverage run, which `is_baseline` leaves out, is not in it."""
    if calls is None:
        return None
    baselines = sum(1 for row in calls if is_baseline(row))
    by_load = sum(1 for report in reports for k in report["kills"]
                  if _by_load(("killed", k.get("code"), k.get("killer", ""))))
    return baselines + by_load


def unreproduced_of(reports: Sequence[dict]) -> list | None:
    """The kills a `--confirm` run could not reproduce, from every report;
    None when a report does not say, as one whose kills were not confirmed
    does not."""
    if not reports or any("unreproduced" not in report for report in reports):
        return None
    return [item for report in reports for item in report["unreproduced"]]


# --------------------------------------------------------------------------
# Verdicts


def verdicts(reports: Sequence[dict]) -> dict[tuple, tuple]:
    """Each mutant of *reports*, by `mutant_key`, and what it came to:
    ("killed", code, killer), ("survived",) or ("accepted",). Two entries
    with one key (a report without diffs, from an engine that wrote none)
    are kept apart by a count, in the order the report lists them, so that
    neither hides the other's verdict."""
    out = {}

    def put(key, said):
        while key in out:
            key = key + ("again",)
        out[key] = said

    for report in reports:
        target = report["target"]
        for k in report["kills"]:
            put(mutant_key(target, k), ("killed", k.get("code"), k.get("killer", "")))
        for e in report["survivors"]:
            put(mutant_key(target, e), ("survived",))
        for e in report["accepted"]:
            put(mutant_key(target, e), ("accepted",))
    return out


def _by_load(said: tuple | None) -> bool:
    """A kill that load can make: by time, or by a signal or a crash."""
    return (said is not None and said[0] == "killed"
            and (said[1] == TIMED_OUT
                 or (said[1] not in PYTEST_CODES and not said[2])))


@dataclasses.dataclass(frozen=True)
class Difference:
    """The mutants two runs disagree on: *load* where one side's verdict is
    a kill load can make (explained, recorded), *real* for the rest."""

    real: list[tuple]
    load: list[tuple]

    def __bool__(self) -> bool:
        return bool(self.real or self.load)


def compare_verdicts(first: dict[tuple, tuple], other: dict[tuple, tuple]) -> Difference:
    """Where *other* disagrees with *first* on a mutant's verdict, the kill's
    killer and code aside: a kill is a kill whichever test made it."""
    real, load = [], []
    for key in sorted(set(first) | set(other), key=repr):
        a, b = first.get(key), other.get(key)
        if (a and a[0]) == (b and b[0]):
            continue
        (load if _by_load(a) or _by_load(b) else real).append((key, a, b))
    return Difference(real, load)


def _said(verdict: tuple | None) -> str:
    if verdict is None:
        return "absent"
    if verdict[0] == "killed":
        return "killed (%s)" % (verdict[2] or "code %s" % verdict[1])
    return verdict[0]


def describe(diff: Difference, first: str, other: str) -> list[str]:
    lines = []
    for kind, found in (("", diff.real), ("load only: ", diff.load)):
        for (target, op, line, change, *_mark), a, b in found:
            lines.append("  %s%s:%s %s %s: %s in %s, %s in %s" % (
                kind, target, line, op, change, _said(a), first, _said(b), other))
    return lines


# --------------------------------------------------------------------------
# The summary and the Finding


@dataclasses.dataclass
class Row:
    """One case at one count of workers, as the summary reports it."""

    case: str
    project: str
    workers: int
    asked: int
    kept: int
    discarded: int
    died: int
    before: float | None
    after: float | None
    whole: float | None
    whole_low: float | None
    whole_high: float | None
    touchable: float | None
    ceiling: float | None
    floor: Floor | None
    verdicts: str
    #: The median pytest processes each side's runs in kept pairs started,
    #: and whether any kept pair's sides started different numbers.
    processes_before: float | None = None
    processes_after: float | None = None
    processes_differ: bool = False
    #: With `--expect-rerun-processes`: "ok", or "missed" when a kept pair's
    #: after run started other than the count expected of it.
    rerun: str = "-"
    #: The median touchable seconds of the before side's runs (U, of the
    #: whole W, which is `before`).
    touchable_part: float | None = None

    @property
    def touchable_needs(self) -> float | None:
        """The touchable speedup the floor allows for (`touchable_floor`)."""
        if self.floor is None:
            return None
        return touchable_floor(self.floor.f, self.before, self.touchable_part)

    @property
    def touchable_beyond(self) -> bool | None:
        if self.floor is None:
            return None
        return touchable_beyond(self.touchable, self.floor.f, self.before,
                                self.touchable_part)

    @property
    def processes(self) -> str:
        if self.processes_before is None or self.processes_after is None:
            return "-"
        said = "%g/%g" % (self.processes_before, self.processes_after)
        if self.processes_differ:
            said += " differ"
        if self.rerun != "-":
            said += " rerun %s" % self.rerun
        return said

    @property
    def conclusive(self) -> bool:
        return self.kept >= self.asked

    @property
    def beyond_floor(self) -> bool | None:
        if self.whole is None or self.floor is None:
            return None
        return abs(self.whole - 1) > 2 * self.floor.f


def median(values: Iterable[float]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _x(value: float | None) -> str:
    return "-" if value is None else "%.2fx" % value


def _s(value: float | None) -> str:
    return "-" if value is None else "%.1f s" % value


def table(rows: Sequence[Row]) -> list[str]:
    """The summary's table: speedups are before over after, so above 1 is
    faster; each a median over the kept pairs, of the ratio within each."""
    head = ["case", "N", "pairs", "before", "after", "speedup", "range",
            "touchable", "ceiling", "f (nbrs)", "beyond 2f", "U", "touch. needs",
            "touch. beyond", "processes", "verdicts"]
    body = []
    for r in rows:
        pairs = "%d/%d" % (r.kept, r.asked)
        if r.discarded or r.died:
            pairs += " (-%d)" % (r.discarded + r.died)
        rng = ("-" if r.whole_low is None
               else "%.2f-%.2f" % (r.whole_low, r.whole_high))
        floor = ("-" if r.floor is None
                 else "%.1f%% (%d)" % (100 * r.floor.f, r.floor.count))
        beyond = {None: "-", True: "yes", False: "no"}[r.beyond_floor]
        if not r.conclusive:
            beyond = "inconclusive"
        needs = ("-" if r.touchable_part is None
                 else "inside f" if r.touchable_needs is None else _x(r.touchable_needs))
        touch = {None: "-", True: "yes", False: "no"}[r.touchable_beyond]
        if not r.conclusive and r.touchable is not None:
            touch = "inconclusive"
        body.append([r.case, str(r.workers), pairs, _s(r.before), _s(r.after),
                     _x(r.whole), rng, _x(r.touchable), _x(r.ceiling), floor,
                     beyond, _s(r.touchable_part), needs, touch, r.processes,
                     r.verdicts])
    widths = [max(len(line[i]) for line in [head] + body) for i in range(len(head))]
    return ["  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip()
            for line in [head] + body]


def _wrap(text: str, prefix: str = "> ") -> list[str]:
    return textwrap.wrap(text, 90 - len(prefix), break_long_words=False,
                         break_on_hyphens=False, initial_indent=prefix,
                         subsequent_indent=prefix)


def finding(info: dict, rows: Sequence[Row]) -> str:
    """The `**Finding:**` block, in CLAUDE.md's form: its date and
    conditions, the numbers as a table, then what they show; quoted, and
    wrapped at 90 as docs/ is.

    *info* holds: date, cpu, cores, os, python, before and after (each a
    dict of ref, sha, settings, env), cases (name -> (command, project,
    where)), deselected (project -> node ids), pairs, workers, calibration
    (project -> what it runs), threshold and fixed; and, when they are
    there, unit ("cold" or "warm") and confirm (a list of dicts of case,
    workers, status and unreproduced)."""
    def side(name):
        s = info[name]
        text = "%s `%s` at `%s`" % (name, s["ref"], s["sha"][:7])
        extra = ["`%s = %s`" % kv for kv in s.get("settings", ())]
        extra += ["`%s=%s`" % kv for kv in s.get("env", ())]
        if extra:
            text += " with %s" % ", ".join(extra)
        return text

    cases = "; ".join("%s is `%s` on %s" % (name, command, where)
                      for name, (command, _project, where) in info["cases"].items())
    deselected = "".join(
        "; every %s run deselects %s (`PYTEST_ADDOPTS`)"
        % (project, ", ".join("`%s`" % node for node in nodes))
        for project, nodes in info["deselected"].items() if nodes)
    calibrated = "; ".join("on %s, %s" % kv for kv in info["calibration"].items())
    unit = ("each run cold" if info.get("unit", "cold") == "cold" else
            "warm: in each slot an untimed run from a fresh `.invective`, then the "
            "timed run")
    conditions = (
        "%s, %s, %s, %s, %s; %s, %s; %s%s; workers %s; %s ABBA pair(s) per case "
        "and N, %s, wall time; a calibration just before and after every run (%s), "
        "a pair discarded past %s; touchable part leaves out %s"
        % (info["date"], info["cpu"], info["cores"], info["os"], info["python"],
           side("before"), side("after"), cases, deselected,
           ", ".join(map(str, info["workers"])), info["pairs"], unit,
           calibrated or "none", info["threshold"], info["fixed"]))
    lines = _wrap("**Finding:** (%s)" % conditions)
    lines.append(">")
    lines.append("> | case | N | pairs | before | after | speedup | touchable | f "
                 "| processes |")
    lines.append("> |---|---|---|---|---|---|---|---|---|")
    for r in rows:
        floor = "-" if r.floor is None else "%.1f%%" % (100 * r.floor.f)
        processes = ("-" if r.processes_before is None or r.processes_after is None
                     else "%g / %g" % (r.processes_before, r.processes_after))
        # "none" for an empty cell: a quoted table row is prose to
        # `test_docs`, which reads a lone `-` between spaces as a dash aside.
        lines.append("> | %s | %d | %d of %d | %s |" % (
            r.case, r.workers, r.kept, r.asked, " | ".join(
                "none" if cell == "-" else cell for cell in (
                    _s(r.before), _s(r.after), _x(r.whole), _x(r.touchable), floor,
                    processes))))
    touched = [r for r in rows if r.touchable is not None]
    if touched:
        # W and U, and what the floor needs of the touchable speedup, so
        # that nobody works the threshold out by hand.
        lines.append(">")
        lines.append("> | case | N | W (before) | U (touchable) | touchable | "
                     "`1 / (1 - 2f W / U)` |")
        lines.append("> |---|---|---|---|---|---|")
        for r in touched:
            needs = r.touchable_needs
            lines.append("> | %s | %d | %s | %s | %s | %s |" % (
                r.case, r.workers, _s(r.before),
                _s(r.touchable_part).replace("-", "none"),
                _x(r.touchable), "inside f" if needs is None else _x(needs)))
    lines.append(">")
    said = []
    for r in rows:
        if r.whole is None:
            said.append("%s at N = %d has no kept pair, so it is inconclusive."
                        % (r.case, r.workers))
            continue
        factor = r.whole if r.whole >= 1 else 1 / r.whole
        how = ("as fast after as before" if round(factor, 2) == 1
               else "%.2fx %s after than before" % (
                   factor, "faster" if r.whole >= 1 else "slower"))
        floor = ("" if r.floor is None else "; floor f %.1f%% from %d neighbour(s)"
                 % (100 * r.floor.f, r.floor.count))
        sentence = ("%s at N = %d runs %s (median of %d pair(s), %.2fx to %.2fx "
                    "before over after%s)"
                    % (r.case, r.workers, how, r.kept, r.whole_low, r.whole_high, floor))
        if r.touchable is not None:
            sentence += ", %.2fx on the touchable part, ceiling %s" % (
                r.touchable, _x(r.ceiling))
        sentence += "."
        if r.touchable is not None:
            needs = r.touchable_needs
            if needs is None:
                sentence += (" With 2f W / U at 1 or more, no speedup of the "
                             "touchable part shows beyond the floor.")
            else:
                sentence += (" The touchable part's floor needs %.2fx (`1 / (1 - 2f W "
                             "/ U)`, W %.1f s, U %.1f s), so the touchable speedup is "
                             "%s it." % (needs, r.before, r.touchable_part,
                                         "beyond" if r.touchable_beyond else "inside"))
        if r.beyond_floor is False:
            sentence += " That is inside 2f, so it is no change."
        if not r.conclusive:
            sentence += " Fewer pairs were kept than asked, so it is inconclusive."
        if r.processes_before is not None and r.processes_after is not None:
            sentence += (" Its sides started %g and %g pytest processes%s." % (
                r.processes_before, r.processes_after,
                ", and in some pair the two differed" if r.processes_differ else ""))
        if r.rerun == "ok":
            sentence += (" Every timed run after started as many as expected of a "
                         "re-run: the baselines, and one a kill by time.")
        elif r.rerun == "missed":
            sentence += (" A timed run after did not start the baselines and one a "
                         "kill by time, as a re-run is expected to.")
        said.append(sentence)
    differ = sorted({r.case for r in rows if r.verdicts == "DIFFER"})
    load = sorted({r.case for r in rows if r.verdicts == "load only"})
    if differ:
        said.append("The verdicts differ between runs of %s." % ", ".join(differ))
    if load:
        said.append("The runs of %s differ only by kills by time or by a signal, put "
                    "down to load." % ", ".join(load))
    if rows and all(r.verdicts == "equal" for r in rows):
        said.append("Every run of a case had the same kills, survivors and "
                    "acceptances.")
    for c in info.get("confirm", ()):
        head = "The %s run at N = %d with `--confirm`" % (c["case"], c["workers"])
        if c["unreproduced"] is None:
            said.append("%s did not say what it could not reproduce (%s)."
                        % (head, c["status"]))
        elif not c["unreproduced"]:
            said.append("%s named no kill it could not reproduce." % head)
        else:
            said.append("%s named %d kill(s) it could not reproduce: %s." % (
                head, len(c["unreproduced"]), ", ".join(
                    "line %s %s" % (u.get("line"), u.get("change"))
                    for u in c["unreproduced"])))
    lines += _wrap(" ".join(said))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# The machine


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _output(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def machine() -> dict:
    """What the Finding names of this machine: CPU model, cores, OS, and the
    power profile and AC state where the OS says them."""
    cpu = physical = None
    system = platform.system()
    if system == "Linux":
        text = _read("/proc/cpuinfo") or ""
        names = re.findall(r"^(?:model name|Model|Hardware)\s*:\s*(.+)$", text, re.M)
        cpu = names[0] if names else None
        cores = set(re.findall(r"^physical id\s*:\s*(\d+)$.*?^core id\s*:\s*(\d+)$",
                               text, re.M | re.S))
        physical = len(cores) or None
    elif system == "Darwin":
        cpu = _output(["sysctl", "-n", "machdep.cpu.brand_string"])
        count = _output(["sysctl", "-n", "hw.physicalcpu"])
        physical = int(count) if count and count.isdigit() else None
    elif system == "Windows":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION"
                                r"\System\CentralProcessor\0") as key:
                cpu = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    cpu = cpu or platform.processor() or "an unnamed CPU"
    logical = os.cpu_count()
    cores = ("%d cores, %d logical CPUs" % (physical, logical) if physical
             else "%s logical CPUs" % logical)
    power = []
    profile = _read("/sys/firmware/acpi/platform_profile")
    if profile:
        power.append("platform profile %s" % profile)
    supplies = "/sys/class/power_supply"
    if os.path.isdir(supplies):
        for name in sorted(os.listdir(supplies)):
            if _read(os.path.join(supplies, name, "type")) == "Mains":
                online = _read(os.path.join(supplies, name, "online"))
                power.append("on AC" if online == "1" else "on battery")
                break
    if system == "Darwin":
        batt = _output(["pmset", "-g", "batt"]) or ""
        if "AC Power" in batt:
            power.append("on AC")
        elif "Battery Power" in batt:
            power.append("on battery")
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    return {"cpu": cpu, "cores": cores, "os": platform.platform(),
            "power": ", ".join(power), "load": load}


# --------------------------------------------------------------------------
# Processes


@dataclasses.dataclass(frozen=True)
class Timed:
    code: int | None
    wall: float
    user: float | None
    sys: float | None
    timed_out: bool


def _children() -> tuple[float, float] | None:
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime, usage.ru_stime


def _stop(proc: subprocess.Popen, passed_on: bool) -> None:
    """End *proc*: give it `GRACE` to end by itself (a ^C at the terminal
    reached it too), then terminate it, then kill it. *passed_on* sends it a
    SIGTERM first, for a signal only this process was sent. A second ^C
    while waiting kills it at once."""
    steps = [proc.terminate, proc.kill]
    if passed_on:
        steps.pop(0)()
    while proc.poll() is None:
        try:
            proc.wait(GRACE)
        except subprocess.TimeoutExpired:
            steps.pop(0)() if steps else proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            proc.wait()


def run_timed(argv: list[str], cwd: str, env: dict, log: str,
              timeout: float | None = None) -> Timed:
    """*argv* run in *cwd*, its output appended to *log*; its exit code, wall
    time, and user and system seconds where `resource` has them. A run past
    *timeout* is terminated and says so. A ^C or SIGTERM ends the run (see
    `_stop`) and is raised again."""
    with open(log, "ab") as out:
        out.write(("$ %s\n" % shlex.join(argv)).encode())
        out.flush()
        before = _children()
        start = time.monotonic()
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        timed_out = False
        try:
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop(proc, passed_on=True)
        except KeyboardInterrupt as exc:
            _stop(proc, passed_on=isinstance(exc, Terminated))
            raise
        wall = time.monotonic() - start
        after = _children()
    user = sys_ = None
    if before and after:
        user, sys_ = after[0] - before[0], after[1] - before[1]
    return Timed(proc.returncode, wall, user, sys_, timed_out)


def status_of(timed: Timed, reported: bool) -> str:
    """What a run of invective came to: "timeout" past `--timeout`;
    "refused" for its exit 2; "ok" when it ran (exit 0 or 1) and wrote its
    report; "died" for anything else, a crash or a signal among them, even
    with a report written."""
    if timed.timed_out:
        return "timeout"
    if timed.code == REFUSED:
        return "refused"
    if timed.code in RAN and reported:
        return "ok"
    return "died"


def check(argv: list[str], what: str, cwd: str | None = None,
          env: dict | None = None, log: str | None = None) -> str:
    """*argv*'s output, or a `BenchError` naming *what* failed, with the end
    of what it said. Appended to *log* as well, when one is given."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise BenchError("%s: cannot start %s: %s" % (what, argv[0], exc)) from exc
    try:
        said, _ = proc.communicate()
    except KeyboardInterrupt as exc:
        _stop(proc, passed_on=isinstance(exc, Terminated))
        raise
    text = said.decode(errors="replace")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("$ %s\n%s" % (shlex.join(argv), text))
    if proc.returncode != 0:
        tail = "\n".join(text.splitlines()[-20:])
        raise BenchError("%s failed (exit %s): %s\n%s"
                         % (what, proc.returncode, shlex.join(argv), tail))
    return text


@contextlib.contextmanager
def sigterm_unwinds():
    """A SIGTERM raises `Terminated` once, so it unwinds as a ^C does."""
    fired = False

    def handler(signum, frame):
        nonlocal fired
        if not fired:
            fired = True
            raise Terminated()

    try:
        previous = signal.signal(signal.SIGTERM, handler)
    except (ValueError, AttributeError):
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextlib.contextmanager
def signals_held():
    """^C and SIGTERM ignored while the temporaries are removed, so that a
    second one cannot leave half of them behind."""
    saved = {}
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            with contextlib.suppress(ValueError):
                saved[signum] = signal.signal(signum, signal.SIG_IGN)
    try:
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


def _rmtree(path: str) -> None:
    def writable(func, target, _exc):
        # Windows will not remove a read-only file, which git's objects are.
        os.chmod(target, 0o700)
        func(target)

    if os.path.lexists(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=writable)
        else:
            shutil.rmtree(path, onerror=writable)


# --------------------------------------------------------------------------
# Settings


def parse_pairs(items: Sequence[str], what: str) -> list[tuple[str, str]]:
    """``KEY=VALUE`` items as (key, value), or a `BenchError` naming *what*."""
    out = []
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise BenchError("%s takes KEY=VALUE, not %r" % (what, item))
        out.append((key.strip(), value))
    return out


def toml_value(value: str) -> str:
    """*value* as TOML: as written when it is a TOML value (true, 4,
    ["a"]), else a string."""
    try:
        tomllib.loads("x = %s" % value)
        return value
    except tomllib.TOMLDecodeError:
        return json.dumps(value)


def insert_into_table(text: str, table: str, lines: Sequence[str]) -> str:
    """*text*, a TOML file, with *lines* at the top of `[table]`, which is
    added at the end when it is not there. The result must parse: a key
    given twice is an error, not the last one winning."""
    header = re.compile(r"^\[%s\]\s*(#.*)?$" % re.escape(table), re.M)
    found = header.search(text)
    if found:
        end = found.end()
        text = text[:end] + "\n" + "\n".join(lines) + text[end:]
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n[%s]\n%s\n" % (table, "\n".join(lines))
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BenchError("[%s] with %s does not parse: %s"
                         % (table, "; ".join(lines), exc)) from exc
    return text


def edit_pyproject(where: str, table: str, lines: Sequence[str]) -> None:
    path = os.path.join(where, "pyproject.toml")
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    text = insert_into_table(text, table, lines)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------------------
# The session


def _venv_python(venv: str) -> str:
    if os.name == "nt":
        return os.path.join(venv, "Scripts", "python.exe")
    return os.path.join(venv, "bin", "python")


def default_cache() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "invective-bench")


@dataclasses.dataclass
class Side:
    label: str
    ref: str
    sha: str = ""
    settings: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    env: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    worktree: str = ""
    python: str = ""
    version: str = ""
    dirs: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Run:
    seq: int
    case: str
    workers: int
    pair: int
    side: str
    timed: Timed | None = None
    status: str = ""
    cal_before: float | None = None
    cal_after: float | None = None
    reports: list[dict] = dataclasses.field(default_factory=list)
    calls: list[dict] | None = None
    marks: dict = dataclasses.field(default_factory=dict)
    log: str = ""
    position: int = 0
    #: "timed", "prime" for a warm slot's untimed first run, or "confirm".
    kind: str = "timed"
    #: A warm slot's untimed first run.
    prime: "Run | None" = None
    #: The pytest processes a re-run is expected to start, when asked.
    expected: int | None = None
    #: What a confirm run could not reproduce; None when it did not say.
    unreproduced: list | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclasses.dataclass
class Calibration:
    key: tuple
    runs: list[float]
    median: float


RUN_COLUMNS = ["seq", "case", "project", "workers", "pair", "position", "side",
               "ref", "commit", "wall", "user", "sys", "cal_before", "cal_after",
               "exit", "status", "killed", "survived", "accepted", "timeouts",
               "runs", "fixed", "marks", "log", "unit", "prime_wall", "prime_exit",
               "prime_status", "prime_killed", "prime_survived", "prime_accepted",
               "prime_timeouts", "prime_runs", "expected_runs", "unreproduced"]
PAIR_COLUMNS = ["case", "workers", "pair", "order", "before_seq", "after_seq",
                "calibrations", "spread", "threshold", "status", "speedup",
                "touchable", "ceiling", "before_fixed", "after_fixed", "before_touchable",
                "before_processes", "after_processes", "same_processes",
                "expected_processes"]
CAL_COLUMNS = ["seq", "case", "project", "command", "runs", "median", "log"]


def _counts(reports: Sequence[dict], prefix: str = "") -> dict:
    """A run's verdicts counted, as runs.tsv's columns."""
    if not reports:
        return {}
    v = verdicts(reports).values()
    return {prefix + "killed": sum(x[0] == "killed" for x in v),
            prefix + "survived": sum(x[0] == "survived" for x in v),
            prefix + "accepted": sum(x[0] == "accepted" for x in v),
            prefix + "timeouts": sum(x[0] == "killed" and x[1] == TIMED_OUT for x in v)}


def _tsv(path: str, columns: list[str], row: dict) -> None:
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        if new:
            fh.write("\t".join(columns) + "\n")
        fh.write("\t".join(_cell(row.get(c)) for c in columns) + "\n")


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return "%.3f" % value
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value).replace("\t", " ").replace("\n", " ")


class Session:
    def __init__(self, args, projects: dict[str, Project], cases: list[Case],
                 workers: list[int], pairs: int, repeats: int,
                 sides: dict[str, Side]) -> None:
        self.args = args
        self.projects = projects
        self.cases = cases
        self.workers = workers
        self.pairs = pairs
        self.repeats = repeats
        self.sides = sides
        self.fixed = [c for c in args.fixed.split(",") if c]
        self.out = os.path.abspath(args.out)
        self.logs = os.path.join(self.out, "logs")
        self.work = ""
        self.repo = args.repo
        self.seq = 0
        self.cal_seq = 0
        self.runs: list[Run] = []
        self.blocks: dict[tuple[str, int], list[str]] = {}
        self.pair_rows: list[dict] = []
        self.sequences: dict[tuple[str, int], list[Run]] = {}
        self.pair_status: dict[tuple[str, int, int], str] = {}
        self.cal_last: Calibration | None = None
        self.threshold = THRESHOLD
        self.threshold_how = "%.2fx" % THRESHOLD
        self.threshold_set = False
        if args.threshold is not None:
            self.threshold, self.threshold_set = args.threshold, True
            self.threshold_how = "%.2fx, as given" % args.threshold
        self.unit = getattr(args, "unit", "cold")
        self.process_failures: list[str] = []
        self.confirmed: list[dict] = []
        self.first_verdicts: dict[str, tuple[Run, dict]] = {}
        self.differences: list[str] = []
        self.differ: dict[str, str] = {}
        self.tests: dict[tuple[str, str], tuple[str, ...]] = {}
        #: Each project's third copy, where the tool's own pytests run.
        self.calibration_dirs: dict[str, str] = {}
        self.machine: dict = {}
        self.stopped = False
        self.started = datetime.datetime.now(datetime.timezone.utc)

    # -- setup

    def setup(self) -> None:
        os.makedirs(self.logs, exist_ok=True)
        self.machine = machine()
        self.work = tempfile.mkdtemp(prefix="invective-bench-", dir=self.args.work)
        setup_log = os.path.join(self.logs, "setup.log")
        for side in self.sides.values():
            side.sha = check(["git", "-C", self.repo, "rev-parse", "--verify",
                              "--end-of-options", side.ref + "^{commit}"],
                             "resolving %s" % side.ref).strip()
        projects = {self.projects[c.project] for c in self.cases}
        requires = sorted({r for p in projects for r in p.requires}
                          | set(self.args.with_))
        for side in self.sides.values():
            print("setting up %s: %s at %s" % (side.label, side.ref, side.sha[:12]),
                  flush=True)
            # Named for the session too (its directory's random part): git
            # names a worktree's entry after its directory, and two sessions
            # at once must not share one.
            side.worktree = os.path.join(self.work, "%s-%s" % (
                os.path.basename(self.work).rpartition("-")[2], side.label))
            check(["git", "-C", self.repo, "worktree", "add", "--detach", "--quiet",
                   side.worktree, side.sha], "making the %s worktree" % side.label,
                  log=setup_log)
            venv = os.path.join(self.work, "venv-" + side.label)
            check(["uv", "venv", "--quiet", "--no-project", "--python",
                   self.args.python, venv], "making the %s venv" % side.label,
                  cwd=self.work, log=setup_log)
            side.python = _venv_python(venv)
            # Compiled now, so that no run, the first calibration above all,
            # pays for compiling a module the next one finds compiled.
            check(["uv", "pip", "install", "--quiet", "--compile-bytecode",
                   "--python", side.python, side.worktree, "pytest", *requires],
                  "installing invective %s into its venv" % side.label,
                  cwd=self.work, log=setup_log)
            side.version = check([side.python, "-VV"], "asking the venv's Python"
                                 ).strip().splitlines()[0]
            freeze = check(["uv", "pip", "freeze", "--python", side.python],
                           "listing the %s venv" % side.label, cwd=self.work)
            with open(os.path.join(self.out, "venv-%s.txt" % side.label), "w",
                      encoding="utf-8") as fh:
                fh.write(side.version + "\n" + freeze)
        for project in sorted(projects, key=lambda p: p.name):
            self._place(project, setup_log)
        for project in sorted(projects, key=lambda p: p.name):
            self._check_deselected(project, setup_log)
        for case in self.cases:
            self._collect(case, setup_log)
        self._warm(setup_log)

    def _warm(self, log: str) -> None:
        """Each calibration run once, untimed: its first run writes the
        tests' rewritten bytecode, and would read as a drift; and a project
        that is not green in the before venv stops here, before any pair."""
        side = self.sides["before"]
        warmed = set()
        for case in self.cases:
            project = self.projects[case.project]
            args = self._calibration_args(case)
            if not args or (project.name, args) in warmed:
                continue
            warmed.add((project.name, args))
            timed = run_timed([side.python, "-m", "pytest", "-q", "-p",
                               "no:cacheprovider", *args],
                              self.calibration_dirs[project.name],
                              self.env_for(side, project), log)
            if timed.code != 0:
                raise BenchError("%s is not green in the before venv (exit %s): "
                                 "`pytest -q %s`; see %s" % (
                                     project.name, timed.code, " ".join(args), log))

    def _place(self, project: Project, log: str) -> None:
        source = None
        if project.repo:
            cache = os.path.join(self.args.cache, project.name + ".git")
            if not os.path.isdir(cache):
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                check(["git", "init", "--quiet", "--bare", cache],
                      "making the cache for %s" % project.name, log=log)
            # Kept under a ref of its own, which says it is there.
            ref = "refs/bench/" + project.commit
            have = subprocess.run(["git", "--git-dir", cache, "rev-parse", "--verify",
                                   "--quiet", ref + "^{commit}"], capture_output=True)
            if have.returncode != 0:
                print("fetching %s at %s into %s" % (project.name, project.commit[:12],
                                                     cache), flush=True)
                check(["git", "--git-dir", cache, "fetch", "--quiet", "--depth", "1",
                       project.repo, "%s:%s" % (project.commit, ref)],
                      "fetching %s" % project.name, log=log)
            source = cache
        for side in self.sides.values():
            if project.self_:
                where = side.worktree
            else:
                where = os.path.join(self.work, side.label, project.name)
                self._copy(project, source, where, log)
            for table, lines in project.pyproject:
                edit_pyproject(where, table, lines)
            if side.settings:
                edit_pyproject(where, "tool.invective",
                               ["%s = %s" % (k, toml_value(v)) for k, v in side.settings])
            side.dirs[project.name] = where
        # **A third copy, for every pytest the tool itself starts**: the
        # calibrations, the collections and the warm-up. What they leave
        # behind that a campaign's copy takes (a `.hypothesis` database, a
        # `.coverage`) would otherwise be in one side's tree and not the
        # other's. Without the side's settings, which no plain run reads.
        where = os.path.join(self.work, "calibration", project.name)
        if project.self_:
            shutil.copytree(self.sides["before"].worktree, where,
                            ignore=shutil.ignore_patterns(".git", "__pycache__",
                                                          ".invective"))
        else:
            self._copy(project, source, where, log)
        for table, lines in project.pyproject:
            edit_pyproject(where, table, lines)
        self.calibration_dirs[project.name] = where

    def _copy(self, project: Project, source: str | None, where: str, log: str) -> None:
        """*project* at *where*: from the cache, by its commit, or copied
        from its `path`."""
        os.makedirs(os.path.dirname(where), exist_ok=True)
        if source:
            # Fetched from the cache, not cloned: a clone of it takes its
            # branches, and it has none.
            check(["git", "init", "--quiet", where], "copying %s" % project.name, log=log)
            check(["git", "-C", where, "fetch", "--quiet", "--depth", "1", source,
                   project.commit], "copying %s" % project.name, log=log)
            check(["git", "-C", where, "checkout", "--quiet", "--detach", project.commit],
                  "checking out %s" % project.name, log=log)
        else:
            shutil.copytree(project.path, where, ignore=shutil.ignore_patterns(
                "__pycache__", ".invective", ".pytest_cache"))

    def env_for(self, side: Side, project: Project) -> dict:
        """The environment of a run on *project* by *side*: this one's, with
        the venv's, the project's deselections and the side's own variables;
        a `PYTEST_ADDOPTS` given there comes after the deselections."""
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        venv = os.path.dirname(os.path.dirname(side.python))
        env["VIRTUAL_ENV"] = venv
        env["PATH"] = os.path.dirname(side.python) + os.pathsep + env.get("PATH", "")
        addopts = [env.pop("PYTEST_ADDOPTS", "")]
        addopts += ["--deselect %s" % node for node in project.deselect]
        for key, value in side.env:
            if key == "PYTEST_ADDOPTS":
                addopts.append(value)
            else:
                env[key] = value
        addopts = " ".join(a for a in addopts if a)
        if addopts:
            env["PYTEST_ADDOPTS"] = addopts
        return env

    def _check_deselected(self, project: Project, log: str) -> None:
        """Each deselected test is out of a collection that goes as a run's
        does, or the case would time it after all."""
        if not project.deselect:
            return
        side = self.sides["before"]
        files = sorted({node.partition("::")[0] for node in project.deselect})
        said = check([side.python, "-m", "pytest", "--collect-only", "-q",
                      "-p", "no:cacheprovider", *files],
                     "collecting %s to check its deselections" % project.name,
                     cwd=self.calibration_dirs[project.name],
                     env=self.env_for(side, project),
                     log=log)
        still = [node for node in project.deselect if node in said.split()]
        if still:
            raise BenchError("%s still collects %s with PYTEST_ADDOPTS deselecting it"
                             % (project.name, ", ".join(still)))

    def _collect(self, case: Case, log: str) -> None:
        """A case's tests: as listed, or the files a collection names, less
        the excluded, as plain paths; the same on both sides."""
        project = self.projects[case.project]
        for side in self.sides.values():
            tests = case.tests
            if case.collect:
                said = check([side.python, "-m", "pytest", "--collect-only", "-q",
                              "-p", "no:cacheprovider", *case.collect],
                             "collecting %s's tests" % case.name,
                             cwd=self.calibration_dirs[project.name],
                             env=self.env_for(side, project), log=log)
                files = sorted({line.partition("::")[0] for line in said.splitlines()
                                if "::" in line})
                tests = tuple(f for f in files
                              if not any(x in f for x in case.collect_exclude))
                if not tests:
                    raise BenchError("collecting %s's tests found none" % case.name)
            self.tests[(case.name, side.label)] = tests
        if self.tests[(case.name, "before")] != self.tests[(case.name, "after")]:
            raise BenchError("%s's tests differ between the sides" % case.name)

    # -- calibration

    def _calibration_args(self, case: Case) -> tuple[str, ...]:
        calibrate = self.projects[case.project].calibrate
        if calibrate == "tests":
            return self.tests[(case.name, "before")]
        return calibrate

    def calibrate(self, case: Case) -> Calibration | None:
        """A calibration for a run of *case*: the last one, when it was taken
        with the same command and no run came after it, else a new one."""
        project = self.projects[case.project]
        args = self._calibration_args(case)
        if not args:
            return None
        key = (project.name, args)
        if self.cal_last is not None and self.cal_last.key == key:
            return self.cal_last
        side = self.sides["before"]
        self.cal_seq += 1
        log = os.path.join(self.logs, "cal-%03d.log" % self.cal_seq)
        argv = [side.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args]
        runs: list[float] = []
        want = project.calibrate_runs
        while True:
            timed = run_timed(argv, self.calibration_dirs[project.name],
                              self.env_for(side, project), log)
            if timed.code != 0:
                raise BenchError("the calibration failed (exit %s), so the project "
                                 "is not green in the before venv; see %s"
                                 % (timed.code, log))
            runs.append(timed.wall)
            if want == "auto":
                if runs[0] > AUTO_ONE_RUN or len(runs) == 3:
                    break
            elif len(runs) >= want:
                break
        self.cal_last = Calibration(key, runs, statistics.median(runs))
        _tsv(os.path.join(self.out, "calibrations.tsv"), CAL_COLUMNS, {
            "seq": self.cal_seq, "case": case.name, "project": project.name,
            "command": shlex.join(["pytest", "-q", "-p", "no:cacheprovider", *args]),
            "runs": [round(r, 3) for r in runs], "median": self.cal_last.median,
            "log": os.path.relpath(log, self.out)})
        return self.cal_last

    # -- one run

    def argv(self, case: Case, side: Side, workers: int, report: str,
             calls: str, marks: str, confirm: bool = False) -> list[str]:
        tests = list(self.tests[(case.name, side.label)])
        argv = [side.python, INSTRUMENT, "--calls", calls, "--marks", marks]
        if case.via == "pytest":
            argv += ["pytest", *case.options, "--mutate", case.target]
            if case.only:
                argv += ["--mutate-only", case.only]
            if case.limit is not None:
                argv += ["--mutate-limit", str(case.limit)]
            argv += ["--mutate-json", report]
            # Left out at one, so a ref from before workers can be compared.
            if workers != 1:
                argv += ["--mutate-workers", str(workers)]
            if confirm:
                argv += ["--mutate-confirm"]
            return argv + tests
        argv += ["run", "--target", case.target, "--tests", *tests]
        if case.only:
            argv += ["--only", case.only]
        if case.limit is not None:
            argv += ["--limit", str(case.limit)]
        argv += ["--json", report]
        if workers != 1:
            argv += ["--workers", str(workers)]
        if confirm:
            argv += ["--confirm"]
        return argv + list(case.options)

    def run_one(self, case: Case, workers: int, pair: int, position: int,
                label: str, done: dict[str, Run], confirm: bool = False) -> Run:
        """One slot of *case* by side *label*, put in *done* as it starts, so
        that a run a ^C stops is recorded with the rest. The slot starts from
        a fresh `.invective`. Cold, it is one timed run; warm (`--unit warm`),
        an untimed run first, whose memory the timed run then has. *confirm*
        makes it the one cold run with `--confirm`."""
        side = self.sides[label]
        project = self.projects[case.project]
        self.seq += 1
        run = Run(self.seq, case.name, workers, pair, label, position=position,
                  kind="confirm" if confirm else "timed")
        done[label] = run
        stem = os.path.join(self.logs, "%03d-%s-n%d-%s%s" % (
            run.seq, case.name, workers, label, "-confirm" if confirm else ""))
        run.log = stem + ".log"
        # Nothing a slot remembers reaches the next, on either side, whether
        # or not the engine keeps anything there.
        _rmtree(os.path.join(side.dirs[project.name], ".invective"))
        print("[%d] %s N=%d %s %s ..." % (
            run.seq, case.name, workers, "confirm" if confirm else "pair %d" % (pair + 1),
            label), end=" ", flush=True)
        self.runs.append(run)
        if not confirm:
            self.sequences.setdefault((case.name, workers), []).append(run)
        try:
            if self.unit == "warm" and not confirm:
                run.prime = Run(run.seq, case.name, workers, pair, label,
                                position=position, kind="prime")
                self._execute(run.prime, case, side, workers, stem + "-prime")
                print("primed in %.1f s (%s)," % (run.prime.timed.wall,
                                                  run.prime.status), end=" ", flush=True)
                if not run.prime.ok:
                    # No timed run is worth taking after an untimed one failed.
                    run.status = run.prime.status
                    print("no timed run", flush=True)
                    print("  the untimed run %s; its log is %s"
                          % (run.status, run.prime.log), flush=True)
                    return run
            self._execute(run, case, side, workers, stem, confirm)
        except KeyboardInterrupt:
            run.status = "stopped"
            if run.prime is not None and not run.prime.status:
                run.prime.status = "stopped"
            print("stopped", flush=True)
            raise
        finally:
            self.cal_last = None
        print("%.1f s, exit %s, %s" % (run.timed.wall, run.timed.code, run.status),
              flush=True)
        if run.status != "ok":
            print("  the run %s; its log is %s" % (run.status, run.log), flush=True)
        return run

    def _execute(self, run: Run, case: Case, side: Side, workers: int, stem: str,
                 confirm: bool = False) -> None:
        """*run* made: timed, and its report, instrument rows and marks read."""
        project = self.projects[case.project]
        run.log = stem + ".log"
        report, calls, marks = (stem + ".report.json", stem + ".calls.jsonl",
                                stem + ".marks.json")
        run.timed = run_timed(
            self.argv(case, side, workers, report, calls, marks, confirm),
            side.dirs[project.name], self.env_for(side, project), run.log,
            self.args.timeout)
        run.status = status_of(run.timed, self._load(run, report, calls, marks))

    def _load(self, run: Run, report: str, calls: str, marks: str) -> bool:
        try:
            with open(report, encoding="utf-8") as fh:
                got = json.load(fh)
            run.reports = got if isinstance(got, list) else [got]
        except (OSError, ValueError):
            run.reports = []
        try:
            with open(calls, encoding="utf-8") as fh:
                run.calls = [json.loads(line) for line in fh if line.strip()]
        except (OSError, ValueError):
            run.calls = None
        # An invective without `Copy` writes no rows: no instrument, not
        # no runs.
        if run.calls == []:
            run.calls = None
        try:
            with open(marks, encoding="utf-8") as fh:
                run.marks = json.load(fh)
        except (OSError, ValueError):
            run.marks = {}
        return bool(run.reports)

    def _record(self, run: Run, case: Case, position: int, fixed: float | None = None):
        side = self.sides[run.side]
        t = run.timed
        prime = run.prime
        row = {
            "seq": run.seq, "case": run.case, "project": case.project,
            "workers": run.workers,
            "pair": "confirm" if run.kind == "confirm" else run.pair + 1,
            "position": position + 1, "side": run.side, "ref": side.ref,
            "commit": side.sha, "wall": t.wall if t else None,
            "user": t.user if t else None, "sys": t.sys if t else None,
            "cal_before": run.cal_before, "cal_after": run.cal_after,
            "exit": t.code if t else None, "status": run.status,
            **_counts(run.reports), "runs": process_count(run.calls),
            "fixed": fixed, "marks": run.marks,
            "log": os.path.relpath(run.log, self.out),
            "unit": "cold" if run.kind == "confirm" else self.unit,
            "expected_runs": run.expected,
            "unreproduced": (None if run.kind != "confirm" else
                             "not said" if run.unreproduced is None
                             else len(run.unreproduced))}
        if prime is not None:
            row.update({"prime_wall": prime.timed.wall if prime.timed else None,
                        "prime_exit": prime.timed.code if prime.timed else None,
                        "prime_status": prime.status,
                        **_counts(prime.reports, "prime_"),
                        "prime_runs": process_count(prime.calls)})
        _tsv(os.path.join(self.out, "runs.tsv"), RUN_COLUMNS, row)

    def _check_verdicts(self, run: Run) -> None:
        mine = verdicts(run.reports)
        if run.case not in self.first_verdicts:
            self.first_verdicts[run.case] = (run, mine)
            self.differ.setdefault(run.case, "equal")
            return
        first, theirs = self.first_verdicts[run.case]
        diff = compare_verdicts(theirs, mine)
        if not diff:
            return
        a = "run %d%s (%s, N=%d)" % (first.seq, " untimed" if first.kind == "prime"
                                     else "", first.side, first.workers)
        b = "run %d%s (%s, N=%d)" % (run.seq, " untimed" if run.kind == "prime"
                                     else "", run.side, run.workers)
        lines = describe(diff, a, b)
        if diff.real:
            self.differ[run.case] = "DIFFER"
            head = "VERDICTS DIFFER: %s %s against %s" % (run.case, b, a)
            banner = "!" * len(head)
            print("\n".join([banner, head, *lines, banner]), file=sys.stderr, flush=True)
        else:
            if self.differ.get(run.case) == "equal":
                self.differ[run.case] = "load only"
            print("verdicts differ only by kills load can make: %s %s against %s"
                  % (run.case, b, a), flush=True)
            print("\n".join(lines), flush=True)
        self.differences += ["%s: %s against %s" % (run.case, b, a), *lines]
        with open(os.path.join(self.out, "verdicts.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.differences) + "\n")

    # -- a pair

    def pair(self, case: Case, workers: int, k: int, sides: tuple[str, str]) -> str:
        """Pair *k* of *case* at *workers*, its sides in the order *sides*:
        what it came to (see `run_block`). Every run it started is recorded,
        a stopped one too, and so is the pair."""
        done: dict[str, Run] = {}
        cals: list[Calibration] = []
        fixed: dict[str, float] = {}
        row = {"case": case.name, "workers": workers, "pair": k + 1,
               "order": " ".join("B" if s == "before" else "A" for s in sides),
               "threshold": self.threshold, "status": "unfinished"}
        try:
            status = "kept"
            for position, label in enumerate(sides):
                before = self.calibrate(case)
                run = self.run_one(case, workers, k, position, label, done)
                run.cal_before = before.median if before else None
                after = self.calibrate(case)
                run.cal_after = after.median if after else None
                if before and not cals:
                    cals.append(before)
                if after:
                    cals.append(after)
                # The untimed run is held to the same verdicts: a run that
                # remembers must come to what the one it remembers did.
                if run.prime is not None and run.prime.ok:
                    self._check_verdicts(run.prime)
                if run.ok:
                    self._check_verdicts(run)
                if run.status == "refused":
                    status = "refused"
                    break
                if not run.ok:
                    status = "died"
            medians = [c.median for c in cals]
            if (status == "kept" and not self.threshold_set
                    and any(len(c.runs) > 1 for c in cals)):
                self.threshold, self.threshold_how = session_threshold(
                    [c.runs for c in cals])
                self.threshold_set = True
                if self.threshold != THRESHOLD:
                    print("the threshold for this session is %s" % self.threshold_how,
                          flush=True)
            if status == "kept" and medians and discarded(medians, self.threshold):
                status = "discarded"
                print("  discarded: calibrations spread %.3fx, past %.2fx"
                      % (spread(medians), self.threshold), flush=True)
            row.update(calibrations=[round(m, 3) for m in medians],
                       spread=spread(medians) if medians else None,
                       threshold=self.threshold, status=status)
            if status in ("kept", "discarded"):
                b, a = done["before"], done["after"]
                # Only at one worker: the runs of several overlap, and their
                # seconds are no part of the wall time to take away.
                if workers == 1 and b.calls is not None and a.calls is not None:
                    keys = fixed_keys(b.reports, self.fixed)
                    fixed = {label: fixed_seconds(done[label].calls, keys, self.fixed)
                             for label in SIDES}
                r = ratios(b.timed.wall, a.timed.wall, fixed.get("before"),
                           fixed.get("after"))
                row.update(speedup=r.whole, touchable=r.touchable, ceiling=r.ceiling,
                           before_fixed=fixed.get("before"),
                           after_fixed=fixed.get("after"),
                           before_touchable=(b.timed.wall - fixed["before"]
                                             if "before" in fixed else None))
                print("  pair %d: %.3fx before over after%s" % (
                    k + 1, r.whole, "" if r.touchable is None
                    else ", %.3fx touchable" % r.touchable), flush=True)
                self._processes(case, workers, k, b, a, row)
            return status
        finally:
            self.pair_status[(case.name, workers, k)] = row["status"]
            row["before_seq"] = done["before"].seq if "before" in done else None
            row["after_seq"] = done["after"].seq if "after" in done else None
            for label in sides:
                if label in done:
                    self._record(done[label], case, done[label].position,
                                 fixed.get(label))
            self.pair_rows.append(row)
            _tsv(os.path.join(self.out, "pairs.tsv"), PAIR_COLUMNS, row)

    def _processes(self, case: Case, workers: int, k: int, b: Run, a: Run,
                   row: dict) -> None:
        """The pair's pytest process counts, the deterministic proxy, put in
        *row* and held to what `--same-processes` and
        `--expect-rerun-processes` ask."""
        counts = {"before": process_count(b.calls), "after": process_count(a.calls)}
        same = (None if None in counts.values()
                else counts["before"] == counts["after"])
        row.update(before_processes=counts["before"], after_processes=counts["after"],
                   same_processes=same)
        where = "%s N=%d pair %d" % (case.name, workers, k + 1)
        print("  pytest processes: %s before, %s after" % (counts["before"],
                                                          counts["after"]), flush=True)
        if getattr(self.args, "same_processes", False) and same is not True:
            self.process_failures.append(
                "%s: before started %s pytest processes, after %s"
                % (where, counts["before"], counts["after"]))
        if getattr(self.args, "expect_rerun_processes", False):
            a.expected = (rerun_expected(a.prime.calls, a.prime.reports)
                          if a.prime is not None else None)
            row["expected_processes"] = a.expected
            if a.expected is None or counts["after"] != a.expected:
                self.process_failures.append(
                    "%s: the timed run after started %s pytest processes, not the "
                    "%s expected of a re-run" % (where, counts["after"], a.expected))

    def run(self) -> None:
        for case in self.cases:
            for workers in self.workers:
                print("\n%s at N = %d: %d pair(s), ABBA, %s" % (
                    case.name, workers, self.pairs, self.unit), flush=True)
                self.blocks[(case.name, workers)] = run_block(
                    self.pairs, self.repeats,
                    lambda k, sides: self.pair(case, workers, k, sides))
        if getattr(self.args, "confirm_run", False):
            for case in self.cases:
                self.confirm(case, max(self.workers))

    def confirm(self, case: Case, workers: int) -> Run:
        """The one recorded run of *case* by the after side at *workers*
        with `--confirm`, cold and untimed for any judgement: what it could
        not reproduce must be nothing."""
        print("\n%s at N = %d with --confirm, recorded" % (case.name, workers),
              flush=True)
        done: dict[str, Run] = {}
        try:
            run = self.run_one(case, workers, -1, 0, "after", done, confirm=True)
            if run.ok:
                run.unreproduced = unreproduced_of(run.reports)
            said = ("not said" if run.unreproduced is None
                    else "none" if not run.unreproduced
                    else "%d kill(s)" % len(run.unreproduced))
            print("  unreproduced: %s" % said, flush=True)
            self.confirmed.append({"case": case.name, "workers": workers,
                                   "status": run.status,
                                   "unreproduced": run.unreproduced})
            return run
        finally:
            if "after" in done:
                self._record(done["after"], case, 0)

    # -- the results

    def rows(self) -> list[Row]:
        ratios_by: dict[tuple[str, int], list[float]] = {}
        for (name, workers), seq in self.sequences.items():
            case = next(c for c in self.cases if c.name == name)
            usable = [(r.side, r.timed.wall if r.timed else 0.0,
                       r.ok and self.pair_status.get((name, workers, r.pair)) == "kept")
                      for r in seq]
            ratios_by.setdefault((case.project, workers), []).extend(neighbours(usable))
        floor = floors(ratios_by)
        rows = []
        for case in self.cases:
            for workers in self.workers:
                if (case.name, workers) not in self.sequences:
                    continue
                kept = [p for p in self.pair_rows if p["case"] == case.name
                        and p["workers"] == workers and p["status"] == "kept"]
                every = [p for p in self.pair_rows if p["case"] == case.name
                         and p["workers"] == workers]
                walls = {label: [r.timed.wall for r in self.sequences[(case.name, workers)]
                                 if r.side == label and r.ok and self.pair_status.get(
                                     (case.name, workers, r.pair)) == "kept"]
                         for label in SIDES}
                speedups = [p["speedup"] for p in kept]
                rerun = "-"
                if getattr(self.args, "expect_rerun_processes", False) and kept:
                    rerun = ("ok" if all(p.get("expected_processes") is not None
                                         and p["expected_processes"]
                                         == p.get("after_processes") for p in kept)
                             else "missed")
                rows.append(Row(
                    case.name, case.project, workers, self.pairs, len(kept),
                    sum(p["status"] == "discarded" for p in every),
                    sum(p["status"] in ("died", "refused") for p in every),
                    median(walls["before"]), median(walls["after"]), median(speedups),
                    min(speedups) if speedups else None,
                    max(speedups) if speedups else None,
                    median(p.get("touchable") for p in kept),
                    median(p.get("ceiling") for p in kept),
                    floor.get((case.project, workers)),
                    self.differ.get(case.name, "-"),
                    median(p.get("before_processes") for p in kept),
                    median(p.get("after_processes") for p in kept),
                    any(p.get("same_processes") is False for p in kept), rerun,
                    median(p.get("before_touchable") for p in kept)))
        return rows

    def info(self) -> dict:
        def side(label):
            s = self.sides[label]
            return {"ref": s.ref, "sha": s.sha, "settings": s.settings, "env": s.env}

        cases = {}
        for case in self.cases:
            project = self.projects[case.project]
            where = ("invective's own worktree" if project.self_
                     else "%s at `%s`" % (project.name, project.commit[:7])
                     if project.commit else "%s (local)" % project.name)
            tests = self.tests.get((case.name, "before"), case.tests)
            if case.collect:
                command = case.command(["$FILES"])
                where += (", where $FILES is every file `pytest --collect-only -q %s` "
                          "names, less %s" % (" ".join(case.collect),
                                              ", ".join(case.collect_exclude) or "none"))
            else:
                command = case.command(tests)
            cases[case.name] = (command, project.name, where)
        calibration = {}
        for case in self.cases:
            project = self.projects[case.project]
            calibrate = project.calibrate
            if not calibrate:
                continue
            what = ("the case's own tests" if calibrate == "tests"
                    else "`pytest -q %s`" % " ".join(calibrate))
            runs = project.calibrate_runs
            what += (", one run past %d s, else the median of three" % AUTO_ONE_RUN
                     if runs == "auto" else ", the median of %d" % runs
                     if runs > 1 else ", one run")
            calibration[project.name] = what
        cores = self.machine.get("cores", "")
        if self.machine.get("power"):
            cores += ", " + self.machine["power"]
        return {
            "date": self.started.date().isoformat(), "cpu": self.machine.get("cpu"),
            "cores": cores, "os": self.machine.get("os"),
            "python": " ".join(self.sides["before"].version.split()[:2])
                      or "Python %s" % self.args.python,
            "before": side("before"), "after": side("after"), "cases": cases,
            "deselected": {self.projects[c.project].name: self.projects[c.project].deselect
                           for c in self.cases},
            "pairs": self.pairs, "workers": self.workers, "calibration": calibration,
            "threshold": self.threshold_how,
            "fixed": ", ".join(self.fixed) or "nothing", "unit": self.unit,
            "confirm": self.confirmed}

    def report(self) -> list[Row]:
        rows = self.rows()
        info = self.info()
        lines = ["invective benchmark, %s" % self.started.isoformat(timespec="seconds"),
                 "before: %s at %s%s" % (self.sides["before"].ref,
                                         self.sides["before"].sha[:12],
                                         self._extras("before")),
                 "after:  %s at %s%s" % (self.sides["after"].ref,
                                         self.sides["after"].sha[:12],
                                         self._extras("after")),
                 "machine: %s; %s; %s; %s" % (info["cpu"], info["cores"], info["os"],
                                             info["python"]),
                 "threshold %s; touchable part leaves out %s; %s units"
                 % (info["threshold"], info["fixed"], self.unit),
                 "speedups are before over after: above 1 is faster; each a median "
                 "over kept pairs of the ratio within each pair", ""]
        lines += table(rows)
        notes = []
        for run in self.runs:
            if run.status not in ("ok", ""):
                notes.append("run %d (%s N=%d %s) %s, exit %s: %s" % (
                    run.seq, run.case, run.workers, run.side, run.status,
                    run.timed.code if run.timed else "-",
                    os.path.relpath(run.log, self.out)))
        for p in self.pair_rows:
            if p["status"] == "discarded":
                notes.append("%s N=%d pair %d discarded: calibrations %s spread %.3fx"
                             % (p["case"], p["workers"], p["pair"], p["calibrations"],
                                p["spread"]))
        for r in rows:
            if not r.conclusive:
                notes.append("%s N=%d is inconclusive: %d of %d pairs kept"
                             % (r.case, r.workers, r.kept, r.asked))
        if self.differences:
            notes.append("verdict differences (verdicts.txt):")
            notes += self.differences
        if self.process_failures:
            notes.append("pytest process counts not met:")
            notes += ["  " + f for f in self.process_failures]
        for c in self.confirmed:
            notes.append("%s N=%d with --confirm (%s): unreproduced %s" % (
                c["case"], c["workers"], c["status"],
                "not said" if c["unreproduced"] is None
                else json.dumps(c["unreproduced"])))
        if self.stopped:
            notes.append("stopped before the schedule ended: these are the pairs run")
        if notes:
            lines += [""] + notes
        text = "\n".join(lines) + "\n"
        with open(os.path.join(self.out, "summary.txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
        block = finding(info, rows)
        with open(os.path.join(self.out, "finding.md"), "w", encoding="utf-8") as fh:
            fh.write(block)
        with open(os.path.join(self.out, "session.json"), "w", encoding="utf-8") as fh:
            json.dump({"argv": sys.argv, "info": info, "machine": self.machine,
                       "blocks": {"%s N=%d" % k: v for k, v in self.blocks.items()},
                       "stopped": self.stopped}, fh, indent=1, default=str)
        print("\n" + text)
        print(block)
        print("written to %s" % self.out)
        return rows

    def _extras(self, label: str) -> str:
        s = self.sides[label]
        bits = ["%s = %s" % kv for kv in s.settings] + ["%s=%s" % kv for kv in s.env]
        return " (%s)" % "; ".join(bits) if bits else ""

    def cleanup(self) -> None:
        """Remove the worktrees, venvs and copies, saying what could not be."""
        if not self.work:
            return
        if self.args.keep:
            print("kept the worktrees, venvs and copies in %s" % self.work)
            return
        stale = False
        for side in self.sides.values():
            if side.worktree and os.path.isdir(side.worktree):
                done = subprocess.run(["git", "-C", self.repo, "worktree", "remove",
                                       "--force", side.worktree], capture_output=True,
                                      text=True)
                if done.returncode != 0:
                    stale = True
                    print("bench: could not remove the worktree %s: %s"
                          % (side.worktree, done.stderr.strip()), file=sys.stderr)
        try:
            _rmtree(self.work)
        except OSError as exc:
            print("bench: could not remove %s: %s" % (self.work, exc), file=sys.stderr)
        if stale:
            # Only then: a prune while another session adds its worktree
            # could take that one's entry for a stale one.
            subprocess.run(["git", "-C", self.repo, "worktree", "prune"],
                           capture_output=True)


# --------------------------------------------------------------------------
# The command line


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bench/compare.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before", help="the git ref to compare against")
    ap.add_argument("after", help="the git ref being measured")
    ap.add_argument("--case", action="append", default=[],
                    help="a case of the cases file; repeat it, or separate with "
                         "commas (default R40)")
    ap.add_argument("--workers", default="1",
                    help="comma-separated counts of workers, each a block of its own "
                         "(default 1)")
    ap.add_argument("--pairs", type=int, default=3,
                    help="pairs to keep for each case and count (default 3)")
    ap.add_argument("--repeats", type=int, default=2,
                    help="most pairs repeated after a discard, per case and count "
                         "(default 2)")
    ap.add_argument("--quick", action="store_true",
                    help="one pair of the first case at the first count, no repeat")
    ap.add_argument("--python", default="3.14",
                    help="the Python of both venvs, as uv takes it (default 3.14)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="a [tool.invective] setting on both sides")
    ap.add_argument("--set-before", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--set-after", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    help="an environment variable on both sides; PYTEST_ADDOPTS "
                         "is added to the project's deselections")
    ap.add_argument("--env-before", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--env-after", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--with", dest="with_", action="append", default=[],
                    metavar="PACKAGE", help="another package for both venvs")
    ap.add_argument("--fixed", default="baseline,timeout",
                    help="what the touchable part leaves out: any of %s (default "
                         "baseline,timeout)" % ",".join(FIXED_CLASSES))
    ap.add_argument("--threshold", type=float, metavar="X",
                    help="discard a pair whose calibrations spread past X, in place "
                         "of %.2f and its adjustment to a noisy first pair" % THRESHOLD)
    ap.add_argument("--timeout", type=float, metavar="SECONDS",
                    help="end a run past this, recorded as a timeout")
    ap.add_argument("--out", help="where the results go (default "
                                  "bench/results/<time>)")
    ap.add_argument("--cases-file", default=CASES, help="default bench/cases.toml")
    ap.add_argument("--cache", default=default_cache(),
                    help="where fetched projects are kept (default %(default)s)")
    ap.add_argument("--work", help="where the temporaries go (default the system's)")
    ap.add_argument("--repo", help="the git repository of the refs (default the "
                                   "one this file is in)")
    ap.add_argument("--unit", choices=("cold", "warm"), default="cold",
                    help="cold: each slot one timed run; warm: an untimed run, then "
                         "the timed one, which remembers it (default cold)")
    ap.add_argument("--same-processes", action="store_true",
                    help="exit 4 unless the two sides of every pair started as many "
                         "pytest processes")
    ap.add_argument("--expect-rerun-processes", action="store_true",
                    help="with --unit warm: exit 4 unless each timed run after starts "
                         "its untimed run's baselines and one run a kill by time")
    ap.add_argument("--confirm-run", action="store_true",
                    help="after the pairs, one recorded run of each case by the after "
                         "side at the largest count of workers with --confirm; exit 5 "
                         "unless it names nothing it could not reproduce")
    ap.add_argument("--setup-only", action="store_true",
                    help="set up, check the deselections and the calibrations, and "
                         "stop before the first pair")
    ap.add_argument("--keep", action="store_true",
                    help="keep the worktrees, venvs and copies")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        projects, cases = load_cases(args.cases_file)
        names = [n for item in (args.case or ["R40"]) for n in item.split(",") if n]
        unknown = [n for n in names if n not in cases]
        if unknown:
            raise BenchError("no case %s in %s; it has %s" % (
                ", ".join(unknown), args.cases_file, ", ".join(cases)))
        try:
            workers = [int(w) for w in args.workers.split(",") if w]
        except ValueError:
            workers = []
        if not workers or min(workers) < 1:
            raise BenchError("--workers takes whole numbers from 1, comma-separated")
        bad = set(args.fixed.split(",")) - set(FIXED_CLASSES) - {""}
        if bad:
            raise BenchError("--fixed takes %s" % ",".join(FIXED_CLASSES))
        pairs, repeats = args.pairs, args.repeats
        if pairs < 1 or repeats < 0:
            raise BenchError("--pairs is at least 1 and --repeats at least 0")
        if args.expect_rerun_processes and args.unit != "warm":
            raise BenchError("--expect-rerun-processes needs --unit warm: a re-run is "
                             "the timed run of a warm slot")
        if args.quick:
            names, workers, pairs, repeats = names[:1], workers[:1], 1, 0
        if args.confirm_run and max(workers) < 2:
            raise BenchError("--confirm-run needs a count of workers above 1, since "
                             "one worker confirms nothing")
        common = parse_pairs(args.set, "--set")
        common_env = parse_pairs(args.env, "--env")
        sides = {
            "before": Side("before", args.before,
                           settings=common + parse_pairs(args.set_before, "--set-before"),
                           env=common_env + parse_pairs(args.env_before, "--env-before")),
            "after": Side("after", args.after,
                          settings=common + parse_pairs(args.set_after, "--set-after"),
                          env=common_env + parse_pairs(args.env_after, "--env-after"))}
        if args.repo is None:
            args.repo = check(["git", "-C", HERE, "rev-parse", "--show-toplevel"],
                              "finding this repository").strip()
        if args.out is None:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            args.out = os.path.join(args.repo, "bench", "results", stamp)
        if os.path.exists(args.out) and os.listdir(args.out):
            raise BenchError("%s is not empty" % args.out)
    except BenchError as exc:
        print("bench: %s" % exc, file=sys.stderr)
        return 2

    print(ADVICE, flush=True)
    session = Session(args, projects, [cases[n] for n in names], workers, pairs,
                      repeats, sides)
    code = 0
    with sigterm_unwinds():
        try:
            session.setup()
            if args.setup_only:
                print("set up: every project is in place and green; no pair run",
                      flush=True)
            else:
                session.run()
        except KeyboardInterrupt:
            session.stopped = True
            code = 130
            print("\nstopped; removing the temporaries", file=sys.stderr, flush=True)
        except BenchError as exc:
            code = 2
            print("\nbench: %s" % exc, file=sys.stderr, flush=True)
        finally:
            with signals_held():
                try:
                    rows = session.report() if session.runs else []
                finally:
                    session.cleanup()
    if code:
        return code
    return exit_status(session, rows)


def exit_status(session, rows: Sequence[Row]) -> int:
    """The exit status of a *session* that ran to its end, saying why when
    it is not 0, the first that applies: 3, verdicts that differ; 4, a
    process count not met; 5, a confirm run that named a kill it could not
    reproduce, or did not say; 1, a run that died or an inconclusive case."""
    if any(v == "DIFFER" for v in session.differ.values()):
        print("bench: the verdicts differ; see %s"
              % os.path.join(session.out, "verdicts.txt"), file=sys.stderr)
        return 3
    if session.process_failures:
        print("bench: pytest process counts not met:\n  %s"
              % "\n  ".join(session.process_failures), file=sys.stderr)
        return 4
    if any(c["unreproduced"] != [] for c in session.confirmed):
        print("bench: the --confirm run named kills it could not reproduce, or did "
              "not say; see summary.txt", file=sys.stderr)
        return 5
    if any(r.status != "ok" for r in session.runs) or not all(r.conclusive for r in rows):
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
