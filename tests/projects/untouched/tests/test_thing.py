from untouched import thing


def test_only_a_lambda():
    """Runs a line of the module, the lambda's, but enters none of its
    functions, so no guard is ever read."""
    assert thing.double(thing.VALUE) == 6


def test_touches_nothing():
    assert 1 + 1 == 2
