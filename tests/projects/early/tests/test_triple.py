from early import triple


def test_one():
    assert triple.triple(1) == 3


def test_two():
    assert triple.triple(2) == 6


def test_three():
    assert triple.triple(3) == 9
