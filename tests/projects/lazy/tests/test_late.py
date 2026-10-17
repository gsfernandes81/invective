"""The one test, which imports `lazy.late` inside its body."""


def test_late():
    """`late.VALUE` is 7, read from a module first imported here, in the child."""
    from lazy import late

    assert late.VALUE == 7
