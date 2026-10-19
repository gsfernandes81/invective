"""A save of the target during the run, made by a test that runs after the others."""

import os

import pytest

#: What the save puts ahead of the file's first function: one with one mutant.
PREPENDED = 'def added() -> bool:\n    """Prepended mid-run."""\n    return True\n\n\n'


def test_save_mid_run(target):
    """Prepend a function to the target, in the baseline only.

    `INVECTIVE_MUTANT` is unset in the engine's baseline child and in plain pytest,
    and set in every child that runs a mutant or the canary, so only the baseline
    edits the file.
    """
    if os.environ.get("INVECTIVE_MUTANT"):
        pytest.skip("a child running a mutant or the canary does not save")
    with open(target, encoding="utf-8") as fh:
        text = fh.read()
    head, _, rest = text.partition("\n\n\n")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(f"{head}\n\n\n{PREPENDED}{rest}")
