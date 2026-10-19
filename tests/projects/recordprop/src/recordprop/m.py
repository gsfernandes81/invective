"""The target of the recordprop project."""

import datetime


def year(when: datetime.date) -> int:
    """Killed by every value: `test_year` checks the year of 1 January 2026.

    The engine refuses the run before any mutant: the test records a
    `datetime.date` with `record_property`, and the baseline child, which cannot
    write that value to JSON, exits 70 without finishing.
    """
    return when.year
