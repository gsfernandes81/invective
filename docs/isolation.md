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
known limits of this.

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
survivor carries `confirmed` as a kill does. A survivor is not run again.

## The remembered killer

With the history on (`history` in `[tool.invective]`, on unless set false), each
mutant whose last killer is remembered (`store.History`) is first run against
that test alone. It counts as a kill only when pytest says a test failed
(`ExitCode.TESTS_FAILED`), the test that failed is the one remembered, and
nothing it was given is missing. Anything else leaves the mutant to the whole
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

## The unsafe speedups

A speedup is unsafe when it can give a wrong verdict on a suite whose tests
depend on their order or are not safe to run in parallel: workers, and the
remembered killer. `--no-unsafe-speedups` (`unsafe-speedups = false`) turns
every one of them off (`config.settle`), so that each mutant is run against the
whole selection in its order, one at a time, and keeps each speedup that cannot
change a verdict on any suite. `config.SPEEDUPS` puts every speedup on its side.
A value in `[tool.invective]` that would turn an unsafe one on gives way to the
switch; a flag that would is refused.

## The import-from-outside refusal

At the end of each run, the plugin checks that the tests loaded the target
from inside the copy (`pytest_invective._loaded_elsewhere`). If not, the
run is refused (exit 2): a mutant written in the copy is never loaded, and
every one survives silently. The usual cause is an editable install whose
`.pth` line puts the project's own `src` on `sys.path`. The fix: pytest's
`pythonpath` setting naming `src`, or a `conftest.py` that puts the copy's
own `src` first on `sys.path` from its own `__file__`.

## A run that tests nothing

A mutant's run of the whole selection that pytest ends with a usage error (exit 4) or
with nothing collected (exit 5), and that names no module as failing, ran no test. It
says nothing of the mutant, and the campaign is refused (`mutate._final`): the baseline
proved this selection collects, so the runner is what broke.

The one exception is a conftest that will not import, which stops pytest with a usage
error before any test, and which the plugin names in the verdict. A conftest that
imports the target fails so whenever a mutant breaks code run at import, and no test can
import the code then: the suite noticed, as it does when a test module will not import.
To tell that mutant from a broken runner, the original is run again on the same selection
in the same copy (`mutate.Copy.original`), on the worker that owns the copy. Green, the
mutant is killed, the conftest is its killer, and the closing lines count it among the
collection or internal errors. Not green, the campaign is refused. Each copy runs the
original again once, at its first such mutant: the original is the same text all
campaign, and every kill made in a copy already rests on its being green there. With
`--confirm`, such a kill has no test to run alone, so the whole selection decides it
again in the first copy, and one that then passes is named as `mutate.NO_KILLER`.

A conftest beside a selection of node ids is loaded as their directory is collected, so
one that will not import there is that directory's collection error, a kill named by the
directory with no run of the original.

An empty selection (exit 5) stays a refusal even when the original passes. A mutant that
renames the tests (a parameter's id) or skips every one leaves nothing to run, and no
test failed on it.

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
