"""A function a session fixture's finalizer calls in a child's final teardown."""


def closing() -> str:
    """Return "ok"; every value is killed by `test_closing`.

    A session fixture's finalizer also calls it and fails the session's last
    teardown with `pytest.fail` when the answer is wrong. With the canary live, the
    finalizer's call raises `Canary`.
    """
    return "ok"
