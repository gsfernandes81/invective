"""The environment variables invective reads or sets, one constant each.

Each constant's docstring names who sets the variable and who reads it, so a reader
can tell a knob the user sets from a signal one of invective's processes sends
another.
"""

KEEP = "INVECTIVE_KEEP"
"""A knob the user sets, read by the engine in the parent at the end of a run.

Set to any non-empty value, it keeps the run's directory (every child's output file
and events file, and the baseline's map) under the temporary directory, for reading
a child's run after the fact. Unset or empty, the engine removes the directory.
"""

MUTANT = "INVECTIVE_MUTANT"
"""The one mutant a pytest run without `--mutate` makes live, as `<path>:<ordinal>`.

The path is the target's file, relative to the directory pytest starts in or
absolute, and the ordinal the mutant's index in that file's table. The display id
does not serve here, since two mutants can share one. Two kinds of process set it:

- a mutant's child, before any test runs, to its target's real path and the mutant's
  ordinal (and the canary child to `canary`), so that every interpreter a test
  starts inherits it;
- a caller that wants one mutant run in a fresh pytest, such as the
  verdict-equality harness's subprocess arm, which sets it in that process's
  environment.

The plugin reads it in a pytest run without `--mutate`, which covers both a pytest
a test starts inside a child and one a caller starts: it installs the finder for
that file and makes the mutant live before any conftest is imported, so a run of
any tests then runs that mutant (`plugin.pytest_load_initial_conftests`). A value
that names no file or no mutant of its table is a usage error, and `canary` makes
nothing live. An interpreter a test starts that is not pytest does not read it and
runs the original code. A run with `--mutate` ignores it.
"""
