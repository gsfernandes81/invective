"""The switch every instrumented module reads: which mutant, if any, is live.

An instrumented module's prologue calls `active` with its own `__file__` and keeps
the answer in its module global `__invective__`. A child makes a mutant live with
`activate`, which sets both: the entry in `ACTIVE` for the target's real path, which
a module first imported in the child reads, and the global of every module of that
path the parent imported. The ordinal is the mutant's index in its module's table,
from 1; `CANARY` is the ordinal of the canary, which raises in every instrumented
function.
"""

from __future__ import annotations

import os
from types import ModuleType

from invective import environment

#: The ordinal that makes every instrumented function raise `Canary`.
CANARY = 0

#: The global every instrumented module holds, and every guard reads.
GLOBAL = "__invective__"

#: A target's real path to the ordinal of its live mutant, read by the prologue of a
#: module of that path when the module is first imported. Keyed by path, so a module
#: imported under any name reads the same entry.
ACTIVE: dict[str, int] = {}

#: A module's `__file__` to its real path, so each file is resolved once.
_REAL: dict[str, str] = {}


class Canary(BaseException):
    """Raised by every instrumented function while the canary is live.

    A `BaseException`, so a test's `except Exception` cannot catch it and let the
    canary pass for a selection that runs the targets.
    """


def active(file: str) -> int | None:
    """Return the ordinal live in the module whose `__file__` is *file*.

    None means the module runs its original code.
    """
    try:
        path = _REAL[file]
    except KeyError:
        path = _REAL[file] = os.path.realpath(file)
    return ACTIVE.get(path)


def activate(path: str, modules: list[ModuleType], ordinal: int, identity: str) -> None:
    """Make *ordinal* live for the target at the real *path*.

    It is live in every module object in *modules*, which the caller passes as the
    target's modules already imported, and in any module of *path* imported after.
    *identity* goes in `environment.MUTANT` for any interpreter a test starts.
    """
    ACTIVE[path] = ordinal
    for module in modules:
        setattr(module, GLOBAL, ordinal)
    os.environ[environment.MUTANT] = identity
