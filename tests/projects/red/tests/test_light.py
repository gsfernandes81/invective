from red import light


def test_stop():
    assert light.stop()


def test_wrong():
    assert light.stop() is False
