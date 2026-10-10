# Isolation

What makes a verdict trustworthy: the mutant on disk in a fresh process,
and the speedups refused because they break that; settled.

## The mutant is the file with one span edited

Each mutant replaces the bytes of the mutated node's span (`lineno` /
`col_offset` to `end_lineno` / `end_col_offset`) in the source file.
Comments, spacing, blank lines and line numbers outside the span are the
file's own, so a traceback in a killed run points at the file's own lines.
Line endings are kept as read (`newline=""`): a CRLF checkout's mutant
differs from the file at the mutation only. An entry whose mutant could not
be written inside its span (a known case: a multi-line `raise` before a
`;`) is written as the whole file unparsed and carries `"whole_file": true`
in the report.

## Each run in a fresh process

Every mutant's tests run as `python -m pytest` in a new process, so
import-time mutants, module-level caches and global state are all seen by
the tests. A child interpreter a test starts imports the mutant when it
starts from the copy's working directory; one started elsewhere imports
whatever copy of the package it finds on `sys.path`. Issue #2 tracks the
known limits of this. A verdict read from the verdict cache (below) is no
run of this campaign's: it was measured so by the run that kept it.

> **Finding:** (2026-10-01, this repo, 4 cores, Python 3.11, pytest 9.1.1)
>
> A fresh pytest process costs about 0.23-0.30 s (`python -c pass` 14 ms,
> `import pytest` 173 ms, pytest on one trivial test 228 ms). A real
> `conftest.py` can push that to 0.5-1.5 s. The engine runs the 6-mutant
> fixture in 0.37 s per run. Copying a 224 KB tree: `shutil.copytree`
> 6 ms, `git worktree add` 14 ms (a wash at this size; both grow with
> the tree's bytes). invective's own suite is the opposite
> case: each test spawns pytest, 0.2-3.5 s per test, so startup is under
> 10% of a run and coverage-guided selection would be worth 10-20x.

## The copy

The copy is of the project as it stands, uncommitted edits and new files
included (`tree.working_tree`). `--ref` checks out a commit instead
(`tree.git_ref`), which is the one use invective makes of git.

What is skipped (`tree.SKIPPED`): `.git`, `.hg`, `.svn`, `.tox`, `.nox`,
`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`,
`node_modules`, `.invective`, and any directory holding `pyvenv.cfg`.

`__pycache__` and `.pytest_cache` matter for correctness: a copied `.pyc`
whose mtime is in the future and whose size matches a mutant's would run
the original's bytecode, and a copied `.pytest_cache` leaks `--lf` state
into the runs. Symlinks are copied as symlinks (`symlinks=True`); a target
reached through a link that leads outside the copy is refused. The copy is
never hardlinked: a hardlink shares the original's inode, so writing a
mutant would edit the project's own file. On Windows, making symlinks
needs a privilege; a file held open by a running process leaves the copy
for the next reaper.

## The marker and the reaper

Every copy carries `.invective-owner` at its top: `{"pid": <owner's pid>,
"root": "<the project>", "ns": "<pid namespace>"}`, written before the copy
is filled. `ns` (`tree._namespace`) is `null` where it cannot be read. Every
`invective run`, every engine the sweep starts, and every `pytest --mutate`
begins by removing copies in the temporary directory whose owner is dead
(`tree.reap`). A copy is kept when its owner is alive, when the marker is
missing or unreadable, when the owner cannot be asked (another user's
process), or when its `ns` is not the reaper's (a container sharing the
temporary directory). A marker without `ns` is judged by its pid alone. Pid
reuse delays a reap, never causes a wrong one.

A copy kept for its `ns` is removed only by a reaper in that same pid
namespace, or by hand: one left by a container that has since gone stays,
and `null` matches only `null`. `ns` tells containers on one host apart,
not hosts: hosts sharing a temporary directory over a network read each
other's pids, as they did before `ns`.

A killed run's copy keeps untracked files such as `.env` until the next
start removes it. A copy being removed is first renamed
`invective-dead-<suffix>`, so a reaper killed halfway through leaves a
name the next reaper recognises. A `--ref` copy's git worktree entry is
cleared before the copy is removed.

## Signals

A run stopped by ^C or SIGTERM removes its copy (`process.stopping_on_sigterm`
turns the signal into an interrupt that unwinds through the copy's
`finally`). A second SIGTERM during the cleanup is ignored, so the removal
is not cut short; SIGKILL is the way to stop a cleanup that hangs, and the
marker covers what that leaves. With more than one worker, a refusal, ^C or
SIGTERM stops every worker's run and removes every copy.

While the workers run (one or more), a ^C or SIGTERM is held where it lands and
raised where the main thread waits for them (`mutate._Held`). Raised where it
lands, it can cut a lock's release short, and a worker that needs the lock to
end would wait for ever, and the removal of the copies with it. A SIGTERM held
with a ^C is the one raised, whichever landed first, so a supervisor's SIGTERM
still ends the process by SIGTERM.

The sweep forwards a SIGTERM to its engine, waits for the engine's copy to
be gone, then dies by the signal itself. `pytest --mutate` ends the
session as interrupted (exit 2) with the copy already gone.

On Windows, `TerminateProcess` ends a process outright; the marker is what
covers a terminated run there. As a pid namespace's init (a container's
entry point), both `invective run` and `invective sweep` remove the copy
and exit 128 + the signal's number.

## Workers

With `workers` above 1, each worker has a copy of its own, made from the first
(`tree.working_tree`'s `source`), or a worktree of the one commit `--ref`
resolves to. The first copy runs the selection alone, then every copy runs it at
once, and each must be green: a selection green alone and red at once is
refused, as one whose tests are not safe to run in parallel or depend on their
order.

The copies' runs go on side by side, so the suite is taken to be one whose
tests are independent of their order and safe to run in parallel, the bar
pytest-xdist sets. A test that holds a port, a fixed path or a database another
copy's run uses can fail and score a kill that is no mutant's, or pass and let
a mutant survive. For a suite that breaks this, the verdicts are not
guaranteed; with `--no-unsafe-speedups`, the runs are one at a time.

Confirmation is the diagnosis, asked for with `--confirm` (`--mutate-confirm`,
`confirm` in `[tool.invective]`). Every kill is then confirmed after the last
mutant has run, with nothing else running, in the first copy restored to the
original: its killer alone, which must also pass alone on the original, or else
the whole selection, whose verdict stands. Before any kill is confirmed, the
whole selection is run there once more, and the run is refused unless it
passes. A kill its killer does not make alone is named in the report
(`unreproduced`) and in the closing lines: its killer is likely a test that
depends on its order or on another copy's run. A kill with no killer to run
alone (a kill by time, by a module, or under `--tests` holding an option) that
the whole selection lets through is named too, as `mutate.NO_KILLER`, and its
survivor carries `confirmed` as a kill does. A survivor is not run again. With
`--confirm` and more than one worker, no kill is read from the verdict cache, so
every kill is run again; a survivor is read, as the run itself takes one from the
pool, and one that confirmation settled is not named again by a later run.

## The remembered killer

With the history on (`history` in `[tool.invective]`, on unless set false), each
mutant whose last killer is remembered (`store.History`) is first run against
that test alone. It counts as a kill only when pytest says a test failed
(`ExitCode.TESTS_FAILED`), the test that failed is the one remembered, and
nothing it was given is missing. Anything else leaves the mutant to its
narrower selection when coverage is on (below), and then to the whole
selection, run as if nothing had been tried.

Before the first mutant, each such test is run alone on the original, at the
mutants' time budget (`mutate._gate`), and only one that passes there, with
nothing missing, is tried. With more than one worker these runs are made side
by side, as the mutants' are. A test the selection does not hold is never
tried: from pytest, the tests it collected; on the command line, the tests the
first baseline kept (`INVECTIVE_INVENTORY`), and only when `--tests` holds
paths and node ids alone, since an option among them (`-k`) would be left out
of a run of one test.

The probe relies on the suite's tests being independent of their order: such
a test fails alone as it fails in the whole selection, so its kill is the whole
selection's. In a suite whose tests depend on their order, it can be a kill the
whole selection in its order would not make. Either way the entry is marked
`"via": "probe"`, and the closing lines count those kills. A test not green
alone on the original, where the whole selection passed, is likely such a
test, and the header's `apart:` line names it (`mutate.NOT_GREEN_APART`).
With `--confirm` and more than one worker, a probe's kill is confirmed like
any other.

## Coverage-guided selection

With `coverage = true` in `[tool.invective]`, one run of the whole selection on
the first copy's original, before the first mutant, records which tests ran
each line of the target (`invective.covers`): in the run, and in every child
interpreter its tests start, which coverage 7.13 and later is started in by its
`.pth` file. It records with invective's own coverage settings, never the
project's, and with none of the user's coverage variables. The tests that ran a
mutant's lines are its narrower selection, unless no line of the mutant ran, a
line ran outside any test (a module's top level, run as it is imported), or
they are more than half the selection.

A mutant with a narrower selection is run against it first, at the mutants'
whole budget (three times the baseline, and at least 30 s), and after its
remembered killer when it has one. No narrower selection is run on the original:
some of the tests of a green selection are green apart, for tests independent of
their order. It is a kill when a test failed (exit 1), the first to fail is one
of the narrower selection's, and none is missing; or when the run is cut at the
budget, since the whole selection holds those tests and would not end inside it
either, while a mutant that only slows them ends inside it here as it would
there. The kill carries `"via": "coverage"`. Anything else (a pass, a module
that would not import, a run that collected nothing) leaves the mutant to the
whole selection, run next in its order, so a survivor is always the whole
selection's verdict (`mutate._SURVIVOR_VIA`).

This relies on the tests being independent of their order, at any count of
workers: a kill by fewer tests than the selection is then a kill by the whole
selection. In a suite whose tests depend on their order, a narrowed kill can be
one the whole selection would not make, by a test that fails apart from the
tests before it; it is marked either way. Such a suite is run with
`--no-unsafe-speedups`, which turns coverage off, or with `coverage` off.
With `--confirm` and more than one worker, a narrowed kill is confirmed as any
other kill is.

A map can be short (a child of another interpreter, one started without the
environment, or one ended by `os._exit`), or credit a line to the wrong test (a
child or a thread that outlives the test that started it). Either costs time,
never a verdict: a narrowed kill still needs one of the narrower selection's
tests to fail on the mutant, or its run to run out the whole budget, and a
mutant they let through goes to the whole selection.

When the map cannot be made or read, the `coverage:` line and the closing lines
say why, and every mutant runs the whole selection: coverage not installed or
older than 7.13, the coverage run red or over ten times the baseline's time (at
least 30 s), `--tests` holding an option a run of fewer tests would leave out,
a path holding a `,` or `$` (coverage's settings split at the one and expand
the other), or a map coverage cannot read.

> **Finding:** (2026-10-10, more-itertools at `81c21a8`, its `test_recipes.py`
> (148 tests) and `recipes.py`, this code at `4487020`, coverage 7.16.2, pytest
> 9.1.1, a 4-vCPU VM with nothing else running; wall time of `mutate.run_tests`
> in the checkout, a plain run and a coverage run in turn, four of each; medians)
>
> | Python | recorder | plain run | coverage run | ratio | lines mapped |
> |---|---|---|---|---|---|
> | 3.11.17 | C tracer | 13.0 s | 71.3 s | 5.5x | 456 |
> | 3.12.3 | `sys.monitoring` | 13.8 s | 14.9 s | 1.08x | 454 |
> | 3.13.16 | `sys.monitoring` | 9.1 s | 10.1 s | 1.11x | 450 |
> | 3.14.6 | `sys.monitoring` | 8.6 s | 9.1 s | 1.06x | 500 |
>
> The coverage runs took 69.9 to 77.5 s on 3.11 and at most 15 s on the others;
> every coverage run on one Python mapped as many lines, credited to as many
> tests. On 3.11, a suite that runs the target hot pays over five times its plain
> run for the map, inside the coverage run's limit of ten times; from 3.12 on, with
> `sys.monitoring` and its events restarted at each test, it pays about a tenth
> more.

> **Finding:** (2026-10-10, the owner's machine: AMD Ryzen AI 9 HX 370, 12 cores, 24
> logical CPUs, on AC, WSL2 with Linux 6.18, Python 3.14.7; more-itertools at
> `81c21a8`, `TestConcurrentTee::test_concurrent_consumers` deselected; R40 is 40
> mutants of `recipes.py` against `test_recipes.py`, RALL all 288; cold runs, wall
> time, a calibration of `pytest -q tests/test_recipes.py` (median of three) around
> every run, a pair discarded past 1.15x. Against `main`: this branch at `fdbc19c`
> with `coverage = true`, `main` at `dcd47a5` at one worker and `7d1dac3` at four,
> one pair per row, `f` 2.0% from no neighbour. The re-fit after the remembered
> killer: `a5d9f60` against `fdbc19c`, both with `coverage = true`, three ABBA pairs
> per row, the median of the ratios within them, `f` 2.0% from two neighbours)
>
> | against | case | N | before | after | speedup | touchable | processes | verdicts |
> |---|---|---|---|---|---|---|---|---|
> | `main` | R40 | 1 | 214.4 s | 203.1 s | 1.06x | 1.15x | 41 / 46 | equal |
> | `main` | RALL | 1 | 1377.6 s | 1260.4 s | 1.09x | 1.16x | 289 / 338 | equal |
> | `main` | R40 | 4 | 66.1 s | 65.1 s | 1.01x | none | 45 / 50 | equal |
> | `main` | RALL | 4 | 369.9 s | 334.9 s | 1.10x | none | 293 / 342 | equal |
> | re-fit | R40 | 1 | 199.5 s | 198.8 s | 1.00x | 1.01x | 46 / 46 | equal |
> | re-fit | R40 | 4 | 65.0 s | 65.5 s | 0.99x | none | 50 / 50 | equal |
>
> | against | case | N | U (touchable) | touchable | its floor needs |
> |---|---|---|---|---|---|
> | `main` | R40 | 1 | 89.7 s | 1.15x | 1.11x |
> | `main` | RALL | 1 | 832.1 s | 1.16x | 1.07x |
>
> At one worker, coverage runs a cold campaign of the recipes suite 6% (R40) to 9%
> (RALL) faster, its touchable part 1.15x and 1.16x, beyond what its floor needs; at
> four workers, 1% and 10%. It starts more pytest processes (the coverage run, and a
> narrowed run before each whole run it does not spare), and every verdict is the
> same. After the remembered killer, it runs as fast as on its own: 1.00x at one
> worker (1.00x to 1.01x across the pairs) and 0.99x at four, with the same
> processes. Not measured: attrs, whose run is refused at its first mutant (issue
> #35), and the 300 mutants of `more.py`.

## The verdict cache

With `cache = true` in `[tool.invective]`, a verdict an earlier run of the same
campaign kept is read instead of a run (`docs/memory.md` has the layout, the key and
the entry). It must never be read where a run under this run's settings could decide
otherwise in the silent direction: a kill the tests do not make, or a survivor lost. So
a verdict is kept only when the whole selection gives it again (`store.sound`), and is
read only by a run that could have decided it (`store.Cache.hit`):

- with `--no-unsafe-speedups`, only a verdict decided with it;
- a verdict decided beside other copies' runs, only at more than one worker;
- with `--confirm` at more than one worker, no kill;
- a kill the remembered killer or the covering tests made alone, only while that
  speedup is in use.

What closes each way a verdict could be read wrongly, and what is left:

| case | closed by | left |
|---|---|---|
| a kill by time, a signal, a crash, the harness, or with no killer | never kept | nothing |
| a kill decided beside other runs, read at one worker | not read at one worker | nothing |
| a verdict of more than one worker, read by `--confirm` | no kill is read; a survivor is, as the run takes its own from the pool, since `--confirm` never runs one again | a survivor confirmation settled is not named again |
| a verdict an unsafe speedup decided, read with the switch | only `safe` entries are read | an entry from a run without the switch that used none of them (one worker, no history, no coverage) is not read either |
| a probe's or a narrowed kill read as the whole selection's | `origin` kept, read only while that speedup is in use, counted on its closing line, never put again | with it in use, a kill read is the kind a fresh run would make, marked as such |
| a kill confirmed alone or by the whole selection | kept as decided with nothing else running, and not `safe` | nothing |
| a survivor | only the whole selection makes one; read whatever the history and coverage, but not when decided beside other runs at one worker, or without the switch with it | one kept with the probe off and read with it on, which a fresh probe could kill: the conservative side |
| a stale input | the key | the limits in `docs/memory.md` |
| a conftest or settings file above the project in a `--ref` worktree | hashed up to the worktree's top | nothing |
| a dependency changed at the same version (a git commit, a rebuild, a reinstall) | its `RECORD` and `direct_url.json` hashed; `sitecustomize` and `usercustomize` among the imported files | a distribution with no `RECORD`; an installed file edited in place |
| the engine changed | its code hashed | nothing |
| the target edited | its text hashed, and each mutant's | nothing |
| a smaller time budget | a kept kill ended inside its budget or failed a test, and a fresh run can only time out or fail the same test; a kept survivor ended inside its budget, and a fresh run that runs out of a smaller one is a kill, so the survivor read is the conservative side | nothing |
| a flaky test, random examples | cannot be told | the verdict sticks: delete `.invective/cache`, and `--confirm` measures every kill again |
| an entry torn, foreign or edited by hand | written whole; both hashes compared; its shape and `store.sound` checked on every read | nothing |
| a store that cannot be written | said once, and nothing more is put | nothing: the cache only saves time |
| two sites whose mutants are the same text | the same program, the same verdict | nothing |
| a run whose tests loaded the target from elsewhere | refused, with no outcome | nothing |

## The unsafe speedups

A speedup is unsafe when it can give a wrong verdict on a suite whose tests
depend on their order or are not safe to run in parallel: workers, the
remembered killer, and coverage-guided selection. `--no-unsafe-speedups`
(`unsafe-speedups = false`) turns every one of them off (`config.settle`), so
that each mutant is run against the whole selection in its order, one at a time,
and keeps each speedup that cannot change a verdict on any suite.
`config.SPEEDUPS` puts every speedup on its side. A value in `[tool.invective]`
that would turn an unsafe one on gives way to the switch; a flag that would is
refused. The verdict cache is safe only restricted, and the engine restricts it,
not `config.settle`: with the switch on, the cache stays on and reads only the
verdicts a run with the switch decided (`store.Cache`), every entry saying whether
it was (`safe`).

## The import-from-outside refusal

At the end of each run, the plugin checks that the tests loaded the target
from inside the copy (`pytest_invective._loaded_elsewhere`). If not, the
run is refused (exit 2): a mutant written in the copy is never loaded, and
every one survives silently. The usual cause is an editable install whose
`.pth` line puts the project's own `src` on `sys.path`. The fix: pytest's
`pythonpath` setting naming `src`, or a `conftest.py` that puts the copy's
own `src` first on `sys.path` from its own `__file__`.

## Pytest settings

Every run goes by what pytest's own search finds in the tree the mutants
are run in. `config.check_pytest_settings` refuses a campaign whose
project reads settings, or a `conftest.py`, from above its top, where no
copy reaches: every run in there would go by other settings, and a mutant
only those settings kill would be reported as a survivor. A settings file
that sets only `markers` is let be: it never changes a verdict silently.
The fix for a workspace member is pytest settings of its own (an empty
`[tool.pytest.ini_options]` table stops pytest's search there). A
`pytest --mutate -c FILE` run skips this check: `-c` is forwarded to every
mutant's run, so they go by the same file.

## Not to build

Each of these trades the on-disk, fresh-process guarantee for a speedup,
and fails in the silent direction invective exists to refuse.

- Mutation switching by environment variable or in-process, warm pytest
  pools and fork servers: they miss import-time mutants, leak state between
  mutants, and do not work on Windows.
- A lightweight runner that calls test functions directly.
- Coverage selection without a confirmation re-run of survivors.
- The last killer put first in the selection's order. A reordered selection
  runs with no gate: a killer that needs an earlier test to have run scores a
  kill that is no mutant's, which the remembered killer's run alone on the
  original catches.
- A cache keyed on the target and the tests only.

> **Finding:** (2026-10-07, or3 benchmark, 4 cores, nothing else running)
>
> `PYTHONDONTWRITEBYTECODE=1` in mutant runs, the only variable changed:
>
> | benchmark | n | off | on | cost | runs |
> |---|---|---|---|---|---|
> | `run`, `cfg.py`, RAISE+CMP+NOT | 18 | 47.5 s | 53.4 s | +12% | 3 |
> | `run`, `farpy.py`, all kinds | 19 | 19.1 s | 20.3 s | +6% | 3 |
> | `sweep`, 3 modules | 22 | 252.6 s | 295.0 s | +17% | 2 |
>
> Verdicts and survivors identical on and off (`cfg.py` 7/18,
> `farpy.py` 9/19). Only the sweep separates clearly; the two `run`
> rows are within noise. The cost is every module a run imports other
> than the mutant, recompiled on every run; the copy already carries no
> `__pycache__`. A second guard that costs 6-17% is not worth it unless
> a filesystem whose mtimes the stamping cannot rely on turns up.
