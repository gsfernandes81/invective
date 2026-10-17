from basic import short


def test_one_line_bodies():
    assert short.one() == 3
    assert short.Box().get() == 4
