"""The inpk fixture project: a package whose tests live inside it.

Under `--import-mode=importlib`, pytest loads this `__init__` itself, from its path,
to import the test module inside the package.
"""


def base() -> int:
    """Killed by every value: the test checks `base()` is 10."""
    return 10
