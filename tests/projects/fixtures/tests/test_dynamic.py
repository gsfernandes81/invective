def test_a_fixture_asked_for_in_the_body(request):
    assert request.getfixturevalue("dyn") == 8
