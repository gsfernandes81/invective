from red import light


def test_stop():
    assert light.stop()


def test_wrong():
    print("the light is red")
    assert light.stop() is False
