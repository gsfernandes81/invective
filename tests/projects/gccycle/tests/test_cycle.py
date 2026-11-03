"""A reference cycle made at collection, and a test that frees it."""

import gc
import weakref

from gccycle import m


class Node:
    """An object that refers to itself."""

    def __init__(self) -> None:
        """Make the cycle: the node is its own `peer`."""
        self.peer = self


#: The only strong reference to the cycle outside itself.
REGISTRY = [Node()]
#: A weak reference to the cycle, dead once the collector frees it.
REF = weakref.ref(REGISTRY[0])


def test_cycle_is_freed():
    """Dropped from the registry, the cycle is freed by a collection; size is 2."""
    REGISTRY.clear()
    gc.collect()
    assert REF() is None
    assert m.size([1, 2]) == 2
