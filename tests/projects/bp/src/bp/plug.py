"""A plugin module a test also loads by path, as a second module named `someplugin`."""


def answer() -> int:
    """Killed by every value: the test checks the loaded copy's `answer()` is 42."""
    return 42


def name() -> str:
    """Killed by both values: a test imports the module and checks `name()`.

    It gives the canary an instrumented function to fire in, so the run is not refused.
    """
    return "plug"
