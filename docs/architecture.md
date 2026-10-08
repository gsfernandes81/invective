# Architecture

The engine, the plugin, and how one mutant is run; settled.

## Modules

| Module | What it is |
|---|---|
| `invective.__main__` | The `invective` command: dispatches `run` and `sweep` |
| `invective.mutate` | The engine: parse, break, run, report |
| `invective.sweep` | Every module in a source tree, each against its tests |
| `invective.tree` | The copy (or git worktree) a campaign's mutants are written in |
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
mutated text, then stamping its mtime one second ahead of the previous
mutant's, so the bytecode cache never reuses the last mutant's `.pyc`.

## One mutant's run

Each mutant is run as `python -m pytest` in the copy, in a process group
of its own (`mutate._OWN_GROUP`), so that stopping the run stops everything
it started. The killer comes from the plugin, which writes it to the file
`INVECTIVE_VERDICT` names, not from the printed output.

## Exit codes

See the exit-code table in `README.md` ("Failing a run on survivors").
