"""One project through the three arms, compared, as a table and a pass or a fail.

The mutant table is taken once, from a copy of the project made before any arm runs,
so a test that edits the project's source changes no arm's idea of the mutants, and
every run, of every arm, is in a fresh copy of that first copy.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import os
import shutil
import tomllib
from typing import NamedTuple

import harness
from harness import arms, compare, edit
from harness.arms import Verdict
from harness.compare import Expected, Mutant, Row
from invective import finder


class HarnessError(Exception):
    """The harness could not run a project: a fault of the harness or its input."""


class Result(NamedTuple):
    """A project's comparison."""

    ok: bool  #: the disagreements found are the expected ones
    table: str


class _Target(NamedTuple):
    """One target file of a project, with its table and its functions' sites."""

    named: str  #: the path as the project names it
    real: str  #: its real path, relative to the copy
    mutants: list[Mutant]
    spans: dict[int, tuple[str, int, int]]  #: an ordinal's site key in `sites`
    sites: dict[tuple[str, int, int], edit.Site]


class _Context(NamedTuple):
    """One project's run of the harness."""

    source: str  #: the copy every other copy is made from
    work: str  #: the directory the copies go in
    files: list[_Target]
    runs: arms.Runs


def jobs() -> int:
    """Return how many pytest processes arms 2 and 3 run at once.

    That is `harness.JOBS` when set, and half the CPUs, at least 2, otherwise.
    """
    given = os.environ.get(harness.JOBS)
    return int(given) if given else max(2, (os.cpu_count() or 2) // 2)


def check(
    project: str,
    work: str,
    expected: Expected,
    targets: list[str],
    args: list[str] | None = None,
) -> Result:
    """Run the three arms on the project directory *project* and compare them.

    *work* is an empty directory for the copies, *expected* the project's expected
    disagreements, *targets* the paths the arms mutate, relative to the project, and
    *args* further pytest arguments every run takes. Raises `HarnessError` when a
    target cannot be read, or when arm 3's probes change the baseline's outcome.
    """
    env = arms.environ(arms.install_recorder(os.path.join(work, "plugins")))
    source = arms.copy(project, os.path.join(work, "source"))
    context = _Context(
        source, work, _targets(source, targets), arms.Runs(args or [], env)
    )
    table = [mutant for target in context.files for mutant in target.mutants]
    workers = sorted({1, *expected.workers, *(e.workers for e in expected.entries)})

    with concurrent.futures.ThreadPoolExecutor(jobs()) as pool:
        base_job = pool.submit(
            arms.baseline,
            _fresh(context, "baseline"),
            context.runs,
            os.path.join(work, "baseline"),
        )
        probe_job = pool.submit(_probe, context)
        engine_jobs = {
            count: pool.submit(
                arms.engine,
                _fresh(context, f"engine-{count}"),
                targets,
                count,
                context.runs,
            )
            for count in workers
        }
        base = base_job.result()
        engines = {count: job.result() for count, job in engine_jobs.items()}
        probed, seen = probe_job.result()
        if base.green and probed.returncode != 0:
            raise HarnessError(f"arm 3's probes changed the baseline:\n{probed.out}")

        verdicts = {count: _engine_verdicts(engines[count], context) for count in workers}
        lines = [
            f"arm 1 with {count} worker{'s' if count > 1 else ''}: "
            f"{engines[count].result} {engines[count].note}".rstrip()
            for count in workers
        ]
        lines.append(
            f"arm 3 baseline: {'green' if base.green else 'red'}, "
            f"{len(base.passed)} passed, {base.seconds:.1f} s {base.note}".rstrip()
        )
        rows = []
        if base.green:
            run = _per_mutant(pool, context, verdicts[1], base)
            rows = [
                _row(mutant, count, verdicts[count].get(mutant), run[mutant], seen)
                for count in workers
                for mutant in table
            ]

    level = _project_level(engines[1].result, base.green)
    differences = compare.unmatched(rows, level, expected, table)
    title = f"{os.path.basename(os.path.normpath(project))}: {len(table)} mutants"
    return Result(not differences, compare.render(title, lines, rows, differences))


def _fresh(context: _Context, name: str) -> str:
    """Return the real path of a fresh copy of the project, under *name*."""
    return arms.copy(context.source, os.path.join(context.work, name, "root"))


def _project_level(engine: str, green: bool) -> str | None:
    """Return arm 1's project-level disagreement with arm 3's baseline, or None."""
    if green:
        return engine if engine in ("refused", "crashed") else None
    return None if engine == "refused" else "ran on a red baseline"


def _row(
    mutant: Mutant,
    workers: int,
    engine: tuple[Verdict, list[str]] | None,
    run: tuple[Verdict, Verdict, str],
    seen: set[str],
) -> Row:
    """Return a mutant's row from its arms' verdicts, with the rules that fire.

    *seen* holds the probe files of arm 3's probe run. Arm 3's survivor whose
    function never ran there is `unreached`.
    """
    sub, truth, probe = run
    if (
        truth.verdict == "survived"
        and not {
            edit.probe_file(probe, edit.IN_TEST),
            edit.probe_file(probe, edit.OUTSIDE),
        }
        & seen
    ):
        truth = Verdict("unreached", note="the function never ran in the probe run")
    found = {"subprocess": sub, "truth": truth}
    if engine is not None:
        found = {"engine": engine[0], **found}
    outside = edit.probe_file(probe, edit.OUTSIDE) in seen
    return Row(mutant, workers, found, compare.rules(found, outside))


def _targets(source: str, targets: list[str]) -> list[_Target]:
    """Return the target files under *targets*, as `plugin.targets_of` would take them.

    A directory stands for its modules that are neither conftests nor test modules,
    outside hidden directories, `__pycache__` and virtual environments. Raises
    `HarnessError` for a target that is missing, unreadable, or outside the project.
    """
    patterns = _python_files(source)
    named: list[str] = []
    for given in targets:
        path = os.path.join(source, given)
        if os.path.isfile(path):
            named.append(os.path.normpath(given))
        elif os.path.isdir(path):
            named += _modules_under(source, path, patterns)
        else:
            raise HarnessError(f"no such target: {given}")

    found: dict[str, _Target] = {}
    for name in named:
        real = os.path.realpath(os.path.join(source, name))
        relative = os.path.relpath(real, source)
        if relative.startswith(os.pardir) or relative in found:
            if relative.startswith(os.pardir):
                raise HarnessError(f"{name} is outside the project: {real}")
            continue
        try:
            info = finder.table(real)
            with open(real, "rb") as fh:
                sites = edit.sites(fh.read())
        except (OSError, SyntaxError, ValueError) as exc:
            raise HarnessError(f"{name} cannot be read as Python: {exc}") from exc
        mutants, spans = [], {}
        for m in info.mutants:
            key = (m.qualname, *m.span)
            if key not in sites:
                raise HarnessError(f"{name}: no function {key} for mutant {m.ordinal}")
            mutants.append(
                Mutant(name, m.ordinal, m.qualname, m.id.partition(":body:")[2])
            )
            spans[m.ordinal] = key
        found[relative] = _Target(name, relative, mutants, spans, sites)
    return [found[key] for key in sorted(found)]


def _python_files(source: str) -> list[str]:
    """Return the project's test module patterns, from its `pyproject.toml`."""
    default = ["test_*.py", "*_test.py"]
    path = os.path.join(source, "pyproject.toml")
    if not os.path.exists(path):
        return default
    with open(path, "rb") as fh:
        options = tomllib.load(fh).get("tool", {}).get("pytest", {})
    patterns = options.get("ini_options", {}).get("python_files", default)
    return patterns.split() if isinstance(patterns, str) else list(patterns)


def _modules_under(source: str, directory: str, patterns: list[str]) -> list[str]:
    """Return the modules a directory target stands for, relative to *source*."""
    found = []
    for top, dirs, names in os.walk(directory):
        dirs[:] = sorted(
            name
            for name in dirs
            if not name.startswith(".")
            and name != "__pycache__"
            and not os.path.exists(os.path.join(top, name, "pyvenv.cfg"))
        )
        found += [
            os.path.relpath(os.path.join(top, name), source)
            for name in sorted(names)
            if name.endswith(".py")
            and name != "conftest.py"
            and not any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
        ]
    return found


def _probe_name(target: _Target, key: tuple[str, int, int]) -> str:
    """Return the probe name of the function *key* of *target*."""
    return f"{sorted(target.sites).index(key)}_{target.real}".replace(os.sep, "_")


def _probe(context: _Context) -> tuple[arms.Ran, set[str]]:
    """Run arm 3's probe run: every target function probed, then plain pytest.

    Returns the run, and the names of the files the probes created.
    """
    root = _fresh(context, "probe")
    seen = os.path.join(context.work, "probe", "seen")
    os.makedirs(seen)
    for target in context.files:
        path = os.path.join(root, target.real)
        with open(path, "rb") as fh:
            text = fh.read()
        wanted = {
            _probe_name(target, key): target.sites[key] for key in target.spans.values()
        }
        with open(path, "wb") as fh:
            fh.write(edit.probed(text, wanted, seen))
    argv = ["-p", "no:invective", *context.runs.args]
    ran = arms.pytest(root, argv, context.runs.env, arms.ENGINE_LIMIT, None)
    return ran, set(os.listdir(seen))


def _engine_verdicts(
    engine: arms.Engine, context: _Context
) -> dict[Mutant, tuple[Verdict, list[str]]]:
    """Return arm 1's verdict and covering tests per mutant, keyed by (path, ordinal).

    The report's rows come in the order of its targets' tables, and each row names
    its file and ordinal; a row that names another mutant than its place gives is a
    row whose verdict was read for a mutant of the same id, and its note says so.
    """
    if engine.result != "ok":
        return {}
    by_real = {target.real: target for target in context.files}
    rows = iter(engine.rows)
    found = {}
    for real in engine.targets:
        target = by_real.get(os.path.normpath(real))
        if target is None:
            raise HarnessError(
                f"arm 1 mutated {real}, which the harness has no table for"
            )
        for mutant in target.mutants:
            row = next(rows, None)
            if row is None:
                raise HarnessError("arm 1's report has fewer rows than the tables")
            note = row["note"]
            if (os.path.normpath(row["file"]), row["ordinal"]) != (real, mutant.ordinal):
                named = f"the row names {row['file']}:{row['ordinal']}"
                note = f"{named}; {note}" if note else named
            verdict = Verdict(row["verdict"], row["killer"], note)
            found[mutant] = (verdict, list(row.get("covering", [])))
    return found


def _per_mutant(
    pool: concurrent.futures.ThreadPoolExecutor,
    context: _Context,
    engine: dict[Mutant, tuple[Verdict, list[str]]],
    base: arms.Baseline,
) -> dict[Mutant, tuple[Verdict, Verdict, str]]:
    """Run arms 2 and 3 for every mutant, *pool*'s size at a time.

    Arm 2 runs the tests arm 1's *engine* verdicts name as covering, or every test
    for a mutant arm 1 left unmeasured or gave no verdict. Returns each
    mutant's arm 2 and arm 3 verdicts, and its function's probe name.
    """
    jobs = {}
    for target in context.files:
        for mutant in target.mutants:
            key = target.spans[mutant.ordinal]
            here = os.path.join(context.work, f"{target.real}-{mutant.ordinal}")
            # An unmeasured mutant's covering tests are suspect: a function that ran
            # at import reaches tests that never call it.
            verdict, covering = engine.get(mutant, (None, []))
            if verdict is None or verdict.verdict == "unmeasured":
                covering = []
            sub = pool.submit(_subprocess, context, here, mutant, covering, base)
            truth = pool.submit(_truth, context, here, target, mutant, base)
            jobs[mutant] = (sub, truth, _probe_name(target, key))
    return {
        mutant: (sub.result(), truth.result(), probe)
        for mutant, (sub, truth, probe) in jobs.items()
    }


def _subprocess(
    context: _Context, here: str, mutant: Mutant, covering: list[str], base: arms.Baseline
) -> Verdict:
    """Run arm 2 for *mutant*, over *covering*, in a copy under *here*."""
    root = arms.copy(context.source, os.path.join(here, "subprocess", "root"))
    try:
        identity = f"{mutant.path}:{mutant.ordinal}"
        return arms.subprocess_arm(root, identity, covering, context.runs, base)
    finally:
        shutil.rmtree(os.path.dirname(root), ignore_errors=True)


def _truth(
    context: _Context, here: str, target: _Target, mutant: Mutant, base: arms.Baseline
) -> Verdict:
    """Run arm 3 for *mutant* of *target*, applied to a copy under *here*."""
    root = arms.copy(context.source, os.path.join(here, "truth", "root"))
    try:
        path = os.path.join(root, target.real)
        with open(path, "rb") as fh:
            text = fh.read()
        site = target.sites[target.spans[mutant.ordinal]]
        with open(path, "wb") as fh:
            fh.write(edit.mutate(text, site, mutant.value))
        return arms.truth_arm(root, context.runs, base)
    finally:
        shutil.rmtree(os.path.dirname(root), ignore_errors=True)
