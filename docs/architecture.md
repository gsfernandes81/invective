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

`mutate._Pool` runs one thread per copy, and every run of a campaign is started
on one of them: the first copy's baseline and every run `--confirm` makes too.
The main thread makes each mutant's text, hands out the work, and prints the
results in site order. It starts no run, because a ^C lands on the main thread,
and one that lands inside `subprocess.Popen` there, after the process is made
and before it is handed back, leaves a run that nothing stops. A run's process
is touched only by the thread that started it.

> **Finding:** (more-itertools at `81c21a8`, Python 3.14, a 4-vCPU Firecracker VM with
> nothing else running; wall time. R40 is `recipes.py` against `test_recipes.py`, 40
> mutants, taken 2026-10-08 with the workers engine at `2cd03f7`, three runs each, the
> median. M60 is `more.py` against `test_more.py`, 60 mutants, taken 2026-10-09 with
> the workers engine at `4c4b100`: the serial engine and one worker two runs each, in
> the order serial, one, one, serial, then four workers two runs, the mean. Every M60
> run deselects `TestConcurrentTee::test_concurrent_consumers` (`PYTEST_ADDOPTS`), whose
> threads took 1.4 to 96 s on this VM while its other vCPUs were idle and a steady 1.5 s
> while they were busy: kept, it timed the VM's scheduler, not invective. The serial
> engine is `4413b08`.)
>
> | case | serial engine | 1 worker | 4 workers | 4 workers, `--confirm` |
> |---|---|---|---|---|
> | R40 | 308.7 s | 306.0 s | 100.2 s | 256.9 s (pool 96.6 s, confirmation 160.1 s) |
> | M60 | 277.4 s | 277.9 s | 85.2 s | not taken |
>
> The M60 runs took 277.2 and 277.6 s on the serial engine, 278.4 and 277.5 s with one
> worker, and 85.7 and 84.7 s with four. Four workers run R40 3.1x and M60 3.3x faster
> than the serial engine, and one worker runs as fast as it. Every run of a case had the
> same kills, survivors and acceptances (M60: 46 killed, 5 of them by time, and 14
> survived). Confirming R40 cost about one serial run of each kill's killer and one full
> run for each kill by time, which took back most of what the workers saved, and found no
> kill its killer did not make alone.

## Exit codes

See the exit-code table in `README.md` ("Failing a run on survivors").
