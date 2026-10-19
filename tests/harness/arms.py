"""The three arms, each run as pytest processes on copies of one project.

- Arm 1, the engine: `pytest --mutate` with the fork path, read from its JSON file.
- Arm 2, the subprocess path: per mutant, a fresh pytest with `environment.MUTANT`
  naming the mutant, over the tests arm 1 found covering it, or every test when
  arm 1 gave none or left the mutant unmeasured.
- Arm 3, ground truth: per mutant, a copy with the mutant applied as a text edit
  (`edit`), and plain pytest with invective's plugin turned off, over every test.

Arms 2 and 3 are judged against arm 3's baseline, an unmutated run of the same
command: a test that passed there and fails or errors now kills the mutant, and the
first such test is named. A run that ends during a test without finishing it (a test
that calls `os._exit`) kills the mutant through that test, which the verdict marks
as not plain. A run over its wall limit is a `timeout`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import NamedTuple

from harness import recorder
from invective import environment

#: A per-mutant run's wall limit is this many times the baseline's, and at least
#: `FLOOR` seconds.
MULTIPLIER = 10.0
FLOOR = 10.0

#: Arm 1's wall limit for the whole run, in seconds.
ENGINE_LIMIT = 900.0

#: What a copy of a project leaves out.
_IGNORED = ("__pycache__", "*.pyc", ".git", ".tox", ".nox", ".pytest_cache", ".venv")

#: Variables the harness's own process may hold that would change a run it starts.
_UNSET = (
    "PYTEST_ADDOPTS",
    "PYTEST_CURRENT_TEST",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_WORKER_COUNT",
    "PYTEST_XDIST_TESTRUNUID",
    environment.MUTANT,
    environment.KEEP,
)


class Verdict(NamedTuple):
    """One arm's verdict on one mutant."""

    verdict: str  #: killed, survived, unreached, unmeasured, timeout, refused, crashed
    killer: str | None = None
    note: str = ""
    #: False for a kill by a run that ended during a test, or a verdict no run gave.
    plain: bool = True


class Ran(NamedTuple):
    """One pytest process, after it ended."""

    returncode: int
    out: str  #: stdout and stderr together
    seconds: float
    timed_out: bool
    lines: list[dict]  #: the recorder's lines


class Engine(NamedTuple):
    """Arm 1's run of a project."""

    result: str  #: ok, refused or crashed
    note: str  #: why, for a run that is not ok
    rows: list[dict]  #: the JSON report's mutant rows, in its order
    targets: list[str]  #: the JSON report's targets, real paths relative to the copy


class Runs(NamedTuple):
    """What every run of one project takes."""

    args: list[str]  #: further pytest arguments
    env: dict[str, str]  #: the environment, from `environ`


class Baseline(NamedTuple):
    """Arm 3's unmutated run of a project."""

    green: bool
    note: str  #: why, for a run that is not green
    passed: frozenset[str]  #: the node ids of the tests that started and did not fail
    seconds: float


def copy(source: str, destination: str) -> str:
    """Copy the project *source* to *destination*, symbolic links kept as links.

    Returns *destination*'s real path, which every run's paths are relative to.
    """

    def ignored(directory: str, names: list[str]) -> set[str]:
        """Return the names a copy leaves out, virtual environments among them."""
        left = set(shutil.ignore_patterns(*_IGNORED)(directory, names))
        left |= {
            name
            for name in names
            if os.path.exists(os.path.join(directory, name, "pyvenv.cfg"))
        }
        return left

    shutil.copytree(source, destination, symlinks=True, ignore=ignored)
    return os.path.realpath(destination)


def environ(plugins: str, **extra: str) -> dict[str, str]:
    """Return the environment of a run, with the recorder importable from *plugins*."""
    env = {k: v for k, v in os.environ.items() if k not in _UNSET}
    path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = plugins + (os.pathsep + path if path else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["COLUMNS"] = "200"
    env.update(extra)
    return env


def install_recorder(directory: str) -> str:
    """Put the recorder in *directory* under its module name, and return *directory*."""
    os.makedirs(directory, exist_ok=True)
    shutil.copy(recorder.__file__, os.path.join(directory, f"{recorder.MODULE}.py"))
    return directory


def pytest(
    root: str, args: list[str], env: dict[str, str], limit: float, results: str | None
) -> Ran:
    """Run `python -m pytest` with *args* in *root*, killed after *limit* seconds.

    The process runs in a session of its own, so the kill reaches every process it
    started. *results* is the recorder's file, read after the run.
    """
    if results is not None:
        env = {**env, recorder.RESULTS: results}
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        env=env,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
    seconds = time.monotonic() - started
    lines = []
    if results is not None and os.path.exists(results):
        with open(results, encoding="utf-8") as fh:
            for line in fh:
                with contextlib.suppress(ValueError):
                    lines.append(json.loads(line))
    return Ran(proc.returncode, out, seconds, timed_out, lines)


def _tail(out: str, count: int = 15) -> str:
    """Return the last *count* lines of *out*."""
    return "\n".join(out.splitlines()[-count:])


def judge(ran: Ran, passed: frozenset[str], limit: float) -> Verdict:
    """Return the verdict of a mutant's run, against the baseline's *passed* tests.

    A test in *passed* that fails or errors in any phase kills the mutant, and so
    does a collection that fails. A test that is skipped, or that never starts, does
    not. A run that ends inside a test, by an exit or an interrupt, kills the mutant
    through that test, and the verdict is marked as not plain.
    """
    if ran.timed_out:
        return Verdict("timeout", note=f"over the wall limit of {limit:.0f} s")
    for line in ran.lines:
        if line["event"] == "collect":
            return Verdict("killed", line["nodeid"] or "<collection>", "collection")
        if (
            line["event"] == "report"
            and line["outcome"] == "failed"
            and line["nodeid"] in passed
        ):
            return Verdict("killed", line["nodeid"], line["when"])
    finish = [line["exitstatus"] for line in ran.lines if line["event"] == "finish"]
    if finish and finish[-1] in (0, 1):
        return Verdict("survived")
    torn_down = {
        line["nodeid"]
        for line in ran.lines
        if line["event"] == "report" and line["when"] == "teardown"
    }
    unfinished = [
        line["nodeid"]
        for line in ran.lines
        if line["event"] == "start" and line["nodeid"] not in torn_down
    ]
    status = finish[-1] if finish else ran.returncode
    if unfinished:
        return Verdict(
            "killed",
            unfinished[-1],
            f"the run ended with status {status} inside the test",
            plain=False,
        )
    return Verdict(
        "crashed",
        note=f"the run ended with status {status}:\n{_tail(ran.out)}",
        plain=False,
    )


def baseline(root: str, runs: Runs, work: str) -> Baseline:
    """Run arm 3's baseline in the copy *root*: plain pytest, nothing mutated.

    The baseline is green when pytest exits 0 having run a test.
    """
    results = os.path.join(work, "baseline.jsonl")
    argv = ["-p", "no:invective", "-p", recorder.MODULE, *runs.args]
    ran = pytest(root, argv, runs.env, ENGINE_LIMIT, results)
    failed = {
        line["nodeid"]
        for line in ran.lines
        if line["event"] == "report" and line["outcome"] == "failed"
    }
    # Every test that started and did not fail, so that a strict xfail, which
    # reports as skipped, is killed by passing under a mutant.
    passed = frozenset(
        line["nodeid"]
        for line in ran.lines
        if line["event"] == "start" and line["nodeid"] not in failed
    )
    green = ran.returncode == 0 and bool(passed)
    note = "" if green else f"pytest exited {ran.returncode}:\n{_tail(ran.out)}"
    return Baseline(green, note, passed, ran.seconds)


def limit_of(base: Baseline) -> float:
    """Return a per-mutant run's wall limit, from the baseline's wall time."""
    return max(FLOOR, MULTIPLIER * base.seconds)


def engine(root: str, targets: list[str], workers: int, runs: Runs) -> Engine:
    """Run arm 1 in the copy *root*: the engine on *targets*, with *workers* children.

    A refusal, exit status 2, is `refused`; any other end without a JSON report, or
    with an exit status other than 0, is `crashed`.
    """
    report = os.path.join(root, f"invective-harness-{workers}.json")
    argv = [f"--mutate={target}" for target in targets]
    argv += ["--mutate-workers", str(workers), "--mutate-json", report, *runs.args]
    ran = pytest(root, argv, runs.env, ENGINE_LIMIT, None)
    if ran.returncode == 2 and "invective refused the run" in ran.out:
        refusal = ran.out[ran.out.index("invective refused the run") :]
        return Engine("refused", refusal.splitlines()[0], [], [])
    if ran.timed_out or ran.returncode != 0 or not os.path.exists(report):
        why = "timed out" if ran.timed_out else f"exited {ran.returncode}"
        return Engine("crashed", f"{why}:\n{_tail(ran.out)}", [], [])
    with open(report, encoding="utf-8") as fh:
        data = json.load(fh)
    return Engine("ok", "", data["mutants"], data["targets"])


def subprocess_arm(
    root: str, mutant: str, covering: list[str], runs: Runs, base: Baseline
) -> Verdict:
    """Run arm 2 in the copy *root*: one pytest with *mutant* live, `<path>:<ordinal>`.

    The run keeps the *covering* tests, in order, or every test when there are none.
    """
    work = os.path.dirname(root)
    extra = {environment.MUTANT: mutant}
    if covering:
        select = os.path.join(work, "select.txt")
        with open(select, "w", encoding="utf-8") as fh:
            fh.writelines(f"{nodeid}\n" for nodeid in covering)
        extra[recorder.SELECT] = select
    limit = limit_of(base)
    ran = pytest(
        root,
        ["-p", recorder.MODULE, *runs.args],
        {**runs.env, **extra},
        limit,
        os.path.join(work, "results.jsonl"),
    )
    if ran.returncode == 2 and "invective refused the run" in ran.out:
        return Verdict("refused", note=_tail(ran.out, 3), plain=False)
    return judge(ran, base.passed, limit)


def truth_arm(root: str, runs: Runs, base: Baseline) -> Verdict:
    """Run arm 3 in the copy *root*, which holds the mutant as an edit: plain pytest."""
    work = os.path.dirname(root)
    limit = limit_of(base)
    ran = pytest(
        root,
        ["-p", "no:invective", "-p", recorder.MODULE, *runs.args],
        runs.env,
        limit,
        os.path.join(work, "results.jsonl"),
    )
    return judge(ran, base.passed, limit)
