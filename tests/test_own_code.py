"""invective's own source, read as a campaign on it reads it.

In no job of the `mutation` workflow: these tests read the module the job
mutates, from the copy that holds the mutant, so a mutant of an accepted
constant would fail them by moving the mutant its acceptance names, and an
acceptance that is true would read as stale.
"""

from __future__ import annotations

import os

import pytest

from invective import accept
from invective import mutate


#: invective's own modules, which accept their survivors in the source.
OWN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), "src")


@pytest.mark.parametrize("module", sorted(
    os.path.relpath(os.path.join(top, name), OWN)
    for top, _dirs, names in os.walk(OWN) for name in names
    if name.endswith(".py")))
def test_every_acceptance_in_invective_s_own_code_names_a_mutant_it_has(
        module):
    """A line wrapped under its acceptance moves the mutant it names to a
    line the acceptance is not about, and the campaign on invective's own
    code says it is stale only when that mutant is among those it runs."""
    with open(os.path.join(OWN, module), encoding="utf-8", newline="") as fh:
        source = fh.read()
    sites = [(node.lineno, what) for _kind, node, what in mutate._sites(
        mutate.ast.parse(source))]
    accepted = accept.read(source, module)
    assert [(a.at, a.change) for a in accepted
            if not any(a.covers(line, what) for line, what in sites)] == []
