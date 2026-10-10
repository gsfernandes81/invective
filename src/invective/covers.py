"""Which tests run which lines of the target: the coverage run's map.

**A mutant can only change what a test sees by being run.** So a mutant
whose lines a test never ran cannot fail that test, and the tests that did
run them are the ones worth running first (`narrowed`). The coverage run
that says which they are is one run of the whole selection, before the
first mutant, with `pytest_invective` recording it in a directory of its
own: the selection's node ids in their order, and coverage's data
for the run and for every child interpreter its tests started, each line
under the number of the test that ran it, or under `""` outside any test.

**A map can only be short, never wrong in the direction that matters.** A
line some test ran but the map does not credit to it (a child coverage did
not see, a test that ran the line and was not recorded) makes a narrower
selection than the truth: one that still has to fail on the mutant, by one
of its own tests, to be a kill, and that hands a mutant it lets through to
the whole selection. A line credited to no test, or to the time outside
any test, gives no narrower selection at all.
"""

from __future__ import annotations

import ast
import glob
import json
import os
from collections.abc import Mapping
from typing import NamedTuple

# The plugin writes the map, so it names its files.
from pytest_invective import COVERAGE_DATA, COVERAGE_TESTS


class Map(NamedTuple):
    """The coverage run's map: the selection's node ids in their order
    (*tests*), and for each line of the target a test ran, the contexts it
    ran under (*lines*): a test's place in *tests* as digits, or `""` for a
    line run outside any test, as while modules are imported."""

    tests: tuple[str, ...]
    lines: Mapping[int, frozenset[str]]


def read(where: str, target: str) -> Map:
    """The map the coverage run recorded in the directory *where*, of the
    file *target*.

    Raises `OSError` or `ValueError` when *where* holds no list of tests,
    and whatever coverage raises for data it cannot read: a map read in
    part would be one nobody asked for.
    """
    # Here and not at the top: the engine runs without coverage installed,
    # and only a campaign that asks for the map needs it.
    from coverage import CoverageData

    with open(os.path.join(where, COVERAGE_TESTS), encoding="utf-8") as fh:
        tests = json.load(fh)
    if not (isinstance(tests, list)
            and all(isinstance(test, str) for test in tests)):
        raise ValueError("%s is not a list of node ids" % COVERAGE_TESTS)
    want = os.path.normcase(os.path.realpath(target))
    lines: dict[int, set[str]] = {}
    pattern = os.path.join(glob.escape(where), COVERAGE_DATA + ".*")
    for path in sorted(glob.glob(pattern)):
        data = CoverageData(basename=path)
        data.read()
        for measured in data.measured_files():
            if os.path.normcase(os.path.realpath(measured)) != want:
                continue
            for line, contexts in data.contexts_by_lineno(measured).items():
                lines.setdefault(line, set()).update(contexts)
    return Map(tuple(tests), {line: frozenset(contexts)
                              for line, contexts in lines.items()})


def narrowed(found: Map, node: ast.AST) -> tuple[str, ...] | None:
    """The tests that ran a line of *node*'s span, in the selection's order;
    None when they are no narrower selection worth a run of its own.

    That is when no line of the span ran, when one ran outside any test
    (a module's top level, run as it is imported, reaches every test that
    imports it), when a context is no test's place, and when they are more
    than half the selection: a mutant they let through is run again
    against the whole selection, so past half, their run and its check on
    the original cost more than they can save.
    """
    ran: set[str] = set()
    for line in range(node.lineno, node.end_lineno + 1):        # type: ignore[attr-defined]
        ran |= found.lines.get(line, frozenset())
    if not ran:
        return None
    places = []
    for context in ran:
        # `""` is not digits, so a line run outside any test ends it here.
        if not context.isdecimal() or int(context) >= len(found.tests):
            return None
        places.append(int(context))
    if 2 * len(places) > len(found.tests):
        return None
    return tuple(found.tests[place] for place in sorted(places))
