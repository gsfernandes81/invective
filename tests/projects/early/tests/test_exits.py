"""A test that ends its process, without a report, when `code` is mutated."""

import os

from early import exits


def test_code():
    """Pass with the original `code`; under a mutant, print and exit with status 5."""
    if exits.code() != 7:
        print("leaving without a verdict", flush=True)
        os._exit(5)
