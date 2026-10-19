"""The three arms agree on every fixture project, but for the disagreements expected.

Each project under `tests/projects/` is one case: the engine, the subprocess path
and plain pytest on hand-applied mutants (the `harness` package) give each mutant a
verdict, and the case passes when the disagreements are exactly those the project's
`expected.toml` lists. The table of every mutant's verdicts is the case's output,
and its failure message.
"""

from __future__ import annotations

import os
import pathlib
import shlex

import harness
import pytest
from harness import check, compare

PROJECTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")


def _cases() -> list[str]:
    """Return the project names, and `external` when the knob names a project."""
    names = sorted(
        name
        for name in os.listdir(PROJECTS)
        if os.path.isdir(os.path.join(PROJECTS, name))
    )
    return names + (["external"] if os.environ.get(harness.PROJECT) else [])


@pytest.mark.parametrize("name", _cases())
def test_the_three_arms_agree_but_for_the_expected_disagreements(
    name: str, tmp_path: pathlib.Path
) -> None:
    """Every disagreement between the arms is expected, and every expected one occurs."""
    if name == "external":
        project = os.environ[harness.PROJECT]
        expected = compare.load(os.environ.get(harness.EXPECTED, os.devnull + ".toml"))
        targets = os.environ.get(harness.TARGETS, "").split()
        args = shlex.split(os.environ.get(harness.ARGS, ""))
    else:
        project = os.path.join(PROJECTS, name)
        expected = compare.load(os.path.join(project, "expected.toml"))
        targets = expected.targets or [os.path.join("src", name)]
        args = []
    for link in expected.links:
        if not os.path.islink(os.path.join(project, link)):
            pytest.skip(f"{link} is not a symbolic link in this checkout")
    result = check.check(project, str(tmp_path), expected, targets, args)
    print(result.table)
    assert result.ok, result.table
