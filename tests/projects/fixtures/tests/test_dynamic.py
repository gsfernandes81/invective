"""A test that asks for a fixture in its body rather than by argument."""

import pytest


def test_a_fixture_asked_for_in_the_body(request: pytest.FixtureRequest):
    """`dyn`, asked for with `getfixturevalue`, is 8."""
    assert request.getfixturevalue("dyn") == 8
