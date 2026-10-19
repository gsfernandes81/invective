"""A module of the package imported the ordinary way, through the import system."""


def name() -> str:
    """Killed by both values: the test checks `name()`.

    It gives the canary an instrumented function to fire in, so the run is not refused.
    """
    return "core"
