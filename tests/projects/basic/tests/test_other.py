from basic import gate, other


def test_twice():
    assert other.twice(2) == 4


def test_sign_and_a_count():
    assert other.sign(1) is True
    assert gate.count(["a", "b"]) != 1
