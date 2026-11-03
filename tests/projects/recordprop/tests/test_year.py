"""A test that records a date as a property of its report."""

import datetime

from recordprop import m


def test_year(record_property):
    """1 January 2026 is in 2026; the date is recorded as the test's `when`."""
    when = datetime.date(2026, 1, 1)
    record_property("when", when)
    assert m.year(when) == 2026
