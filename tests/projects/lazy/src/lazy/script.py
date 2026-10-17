"""A script no test imports; importing it exits.

The parent takes its mutants from the source without running it, and every mutant
is unreached.
"""


def main() -> int:
    """Return 0; no test calls it."""
    return 0


raise SystemExit(3)
