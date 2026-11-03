"""A property a module constant reads, so its getter runs at import and its setter not.

The getter and the setter share the qualname `C.v`. Each docstring gives the verdict
plain pytest gives.
"""

from __future__ import annotations


class C:
    """A value behind a property with a getter and a setter."""

    def __init__(self) -> None:
        """Start at 3; a constructor has no mutant."""
        self._v = 3

    @property
    def v(self) -> int:
        """Killed by every value, through `DEFAULT`, which reads it at import."""
        return self._v

    # The setter returns an int so that its mutants' ids are the getter's.
    @v.setter  # ty: ignore[invalid-argument-type]
    def v(self, value: int) -> int:
        """Killed by every value: a test sets `v` and reads the store each one skips."""
        self._v = value
        return 0


DEFAULT = C().v
