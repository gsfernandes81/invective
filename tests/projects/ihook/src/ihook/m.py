"""The target of the ihook project, which a test in a sibling directory calls."""


def tag(name: str) -> str:
    """Killed by both values: `test_tag` checks `tag("a") == "[a]"`.

    The engine refuses the run as red before any mutant: its children run each test
    through the hooks of the test's own directory, so the wrapper in
    `tests/wrapped/conftest.py` never runs for `test_tag`, which then fails.
    """
    return "[" + name + "]"
