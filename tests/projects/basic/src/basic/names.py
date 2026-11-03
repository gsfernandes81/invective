"""A module the tests reach under a second name and through a reload.

`basic.names` and the top-level `names`, imported once a test puts this directory
on `sys.path`, are two module objects of one file. A reload runs the file again in
the module it is given. The mutant live in the file is live in each.
"""

from __future__ import annotations


def greet(name: str) -> str:
    """Killed by both values: only the test importing the top-level `names` calls it."""
    return "hello " + name


def part(name: str) -> str:
    """Killed by both values: only the test that reloads `basic.names` calls it."""
    return "goodbye " + name
