"""Weak tests of `shadow.m`, which check each result's type and nothing more."""

from shadow import m


def test_chunks_are_lists():
    """Each chunk of [1, 2, 3] is a list; an empty iterator passes too."""
    for chunk in m.chunks([1, 2, 3]):
        assert isinstance(chunk, list)


def test_initials_are_a_set():
    """The initials of two names are a set."""
    assert isinstance(m.initials(["ann", "bob"]), set)


def test_distinct_is_a_frozenset():
    """The distinct names of a list with a repeat are a frozenset."""
    assert isinstance(m.distinct(["ann", "ann"]), frozenset)
