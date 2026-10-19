"""A function the tests reach through parametrised cases with awkward ids.

The ids hold brackets, a slash, spaces, `::`, non-ASCII letters, and a duplicate,
and a kill names its test by an id that holds them.
"""

from __future__ import annotations


def tidy(text: str) -> str:
    """Killed by both values; `''` only by the case whose id holds `::` and `ï`."""
    return text.strip()
