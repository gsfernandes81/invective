"""Two tests that import `lazy2.late` inside their bodies; the first imports it."""


def test_a_imports_first():
    """`other()` is 1; the module is first imported here."""
    from lazy2 import late

    assert late.other() == 1


def test_b_reads_value():
    """`VALUE` is 7, read from the module the first test imported."""
    from lazy2 import late

    assert late.VALUE == 7
