"""The switch every instrumented module reads: which mutant, if any, is live.

An instrumented module's prologue calls `active` with its own `__file__` and
keeps the answer in its module global `__invective__`. A child that
activates a mutant sets both: the entry here for the target's real path,
which a module first imported in the child reads, and the global of every
module of that path already imported in the parent. The ordinal is the
mutant's index in its module's table, from 1; `CANARY` is the synthetic
mutant that raises in every instrumented function.
"""

from __future__ import annotations

import os
from types import ModuleType

#: The ordinal that makes every instrumented function raise `Canary`.
CANARY = 0

#: A target's real path to the ordinal of its live mutant, read by the
#: prologue of a module of that path when it is first imported. Keyed by
#: path, so that a module imported under any name reads the same entry.
ACTIVE: dict[str, int] = {}

#: The environment variable named for an interpreter a test starts. 1.0's
#: finder is not installed in it, so it sees the original code; the variable
#: is set so that the startup hook, when it comes, needs no new handshake.
ENV = "INVECTIVE_MUTANT"

_REAL: dict[str, str] = {}


class Canary(BaseException):
    """Raised by every instrumented function while the canary is live.

    A `BaseException`, so that a test's `except Exception` cannot swallow
    it and let the canary pass for a selection that does run the targets.
    """


def active(file: str) -> int | None:
    """The ordinal live in the module whose `__file__` is *file*, or None
    when it runs its original code."""
    try:
        path = _REAL[file]
    except KeyError:
        path = _REAL[file] = os.path.realpath(file)
    return ACTIVE.get(path)


def activate(path: str, modules: list[ModuleType], ordinal: int, identity: str) -> None:
    """Make *ordinal* live for the target at the real *path*: in every module
    object of it given, already imported, and in any imported after.

    *identity* is what an interpreter a test starts would read from `ENV`.
    """
    ACTIVE[path] = ordinal
    for module in modules:
        setattr(module, "__invective__", ordinal)
    os.environ[ENV] = identity
