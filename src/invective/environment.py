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
"""A signal from a child to the interpreters its tests start; the user does not set it.

The child sets it, before any test runs, to the id of the mutant live in it, or to
`canary` in the canary child, and an interpreter a test starts inherits it. No
process reads it yet: the finder is not installed in such an interpreter, which runs
the original code. It is set so that the startup hook planned for that interpreter
needs no new handshake.
"""
