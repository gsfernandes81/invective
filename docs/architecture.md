# Architecture

The engine, the plugin, and how one mutant is run; settled.

## Modules

| Module | What it is |
|---|---|
| `invective.__main__` | The `invective` command: dispatches `run` and `sweep` |
| `invective.mutate` | The engine: parse, break, run, report |
| `invective.sweep` | Every module in a source tree, each against its tests |
| `invective.tree` | The copy (or git worktree) a campaign's mutants are written in |
| `invective.process` | The processes invective starts: their groups, how they are stopped, and SIGTERM |
| `invective.accept` | Survivors accepted in the source, beside the code they are about |
| `invective.config` | The `[tool.invective]` table, the project's top, and the pytest settings check |
| `invective.errors` | `Refusal`: the one way invective declines to run |
| `pytest_invective` | The pytest plugin: `--mutate`, the verdict, and the loaded-elsewhere check |

## The plugin is a separate package

`pytest_invective` is a package of its own, beside `invective` rather than
inside it. Its docstring states why: pytest loads plugins before a
repository's `pythonpath` setting takes effect (before 8.4) and before any
`conftest.py`, so a package imported by the plugin comes from wherever
invective is installed. In a run of invective against its own code, that is
the checkout, not the copy that holds the mutant, and every mutant would
survive. The plugin imports nothing from `invective` at the top level.

## The handshake

The engine and the plugin communicate through environment variables, each
popped so a pytest the suite starts does not inherit them:

- `INVECTIVE_SELECTION`: a file of node ids, one a line, that the plugin
  keeps and no others.
- `INVECTIVE_VERDICT`: a file the plugin writes at the session's end with
  the first failing test's node id, the selected tests that were not found,
  and whether the target was loaded from outside the copy.
- `INVECTIVE_TARGET`: the mutated module's path from the copy's top, `/`
  separators. The plugin checks the tests loaded it from inside the copy.
- `INVECTIVE_TYPED`: how many trailing arguments are paths for pytest's
  settings search, not tests to collect.

## The copy

`tree.working_tree` copies the project as it stands into a temporary
directory. `tree.git_ref` checks out a commit as a detached git worktree.
Both are context managers that yield the path of the copy's top, remove it
on exit (including after a SIGTERM), and begin by removing copies whose
owner is dead (`tree.reap`). What is skipped: `tree.SKIPPED` (version
control, caches, `node_modules`) and any directory holding `pyvenv.cfg`.
`[tool.invective] exclude` leaves out anything else.

A mutant is written by replacing the target file in the copy with the
mutated text, then stamping its mtime at the campaign's start plus the mutant's
place in the file, in seconds, so each copy's mtimes rise with every mutant it
is given.

## One mutant's run

Each mutant is run as `python -m pytest` in the copy, in a process group
of its own (`process.OWN_GROUP`), so that stopping the run stops everything
it started. The killer comes from the plugin, which writes it to the file
`INVECTIVE_VERDICT` names, not from the printed output.

## Workers

`mutate._Pool` runs one thread per copy. The main thread makes each mutant's
text, hands out the work, and prints the results in site order; a run's process
is touched only by the thread that started it.

> **Finding:** (2026-10-08, more-itertools at `81c21a8`, Python 3.14, a 4-vCPU VM,
> three runs each, median wall time; R40 is `recipes.py` against `test_recipes.py`,
> 40 mutants; M60 is `more.py` against `test_more.py`, 60 mutants)
>
> | case | serial engine | 1 worker | 4 workers | 4 workers, `--confirm` |
> |---|---|---|---|---|
> | R40 | 308.7 s | 306.0 s | 100.2 s | 256.9 s (pool 96.6 s, confirmation 160.1 s) |
> | M60 | 322.7 s | 952.1 s | 96.7 s | 2127.0 s (pool 264.7 s, confirmation 1862.3 s) |
>
> Four workers run R40 3.1x and M60 3.3x faster than the serial engine, with the
> same kills, survivors and acceptances. The VM's speed varied through the day:
> the M60 serial runs beside these took 2036 to 2164 s, one-worker runs 426 to
> 1252 s, and two of those scored 5 and 1 more kills, by time, so M60's serial
> figure is the day's first run, on a quiet hour, and its `--confirm` figure is
> one run on a slow one. Confirming costs about one serial run of each kill's
> killer and one full run for each kill by time, which takes back most of what
> the workers save; it found no kill its killer did not make alone on either
> suite.

## Exit codes

See the exit-code table in `README.md` ("Failing a run on survivors").
