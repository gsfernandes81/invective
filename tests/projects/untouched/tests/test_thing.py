"""Tests that run no instrumented function of the target."""

from untouched import thing


def test_only_a_lambda():
    """Runs a line of the module, the lambda's, but enters none of its functions.

    No guard is read, so the canary passes.
    """
    assert thing.double(thing.VALUE) == 6


def test_touches_nothing():
    """Runs no line of the target."""
    assert 1 + 1 == 2
