"""The test of `ids`, parametrised under awkward ids."""

import pytest
from basic import ids


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("  ", "", id="[brackets]"),
        pytest.param("\t", "", id="a/slash"),
        pytest.param(" \n", "", id="with spaces"),
        pytest.param("", "", id="twice"),
        pytest.param(" ", "", id="twice"),
        pytest.param(" café ", "café", id="naïve::café"),
    ],
)
def test_tidy(text, expected):
    """Tidied text has no space at either end."""
    assert ids.tidy(text) == expected
