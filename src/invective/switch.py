"""The switch every instrumented module reads: which mutant, if any, is live.

An instrumented module's prologue calls `active` with its own name and keeps
the answer in its module global `__invective__`. A child that activates a
mutant sets both: the global of every module already imported, and the entry
here, which a module imported later reads. The ordinal is the mutant's index
in its module's table, from 1; `CANARY` is the synthetic mutant that raises
in every instrumented function.
"""

from __future__ import annotations

import os

#: The ordinal that makes every instrumented function raise `Canary`.
CANARY = 0

#: Module name to the ordinal of its live mutant. Read by a module's
#: prologue when it is first imported, so it is per module name.
ACTIVE: dict[str, int] = {}

#: The environment variable named for an interpreter a test starts. 1.0's
#: finder is not installed in it, so it sees the original code; the variable
#: is set so that the startup hook, when it comes, needs no new handshake.
ENV = "INVECTIVE_MUTANT"


class Canary(BaseException):
    """Raised by every instrumented function while the canary is live.

    A `BaseException`, so that a test's `except Exception` cannot swallow
    it and let the canary pass for a selection that does run the targets.
    """


def active(module: str) -> int | None:
    """The ordinal live in *module*, or None when it runs its original code."""
    return ACTIVE.get(module)


def activate(modules: list[object], names: list[str], ordinal: int | None,
             identity: str) -> None:
    """Make *ordinal* live in every module object and name given.

    *identity* is what an interpreter a test starts would read from `ENV`.
    """
    for module in modules:
        setattr(module, "__invective__", ordinal)
    for name in names:
        if ordinal is None:
            ACTIVE.pop(name, None)
        else:
            ACTIVE[name] = ordinal
    if ordinal is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = identity
