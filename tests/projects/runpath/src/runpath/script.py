"""A script that exits with `code()`; a test runs it with `runpy.run_path`."""


def code() -> int:
    """Killed by every value: the test checks the script exits 3."""
    return 3


def name() -> str:
    """Killed by both values: a test imports the module and checks `name()`.

    It gives the canary an instrumented function to fire in, so the run is not refused.
    """
    return "script"


if __name__ == "__main__":
    raise SystemExit(code())
