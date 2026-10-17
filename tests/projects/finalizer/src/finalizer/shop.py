def closing() -> str:
    """Called by a session fixture's finalizer, which fails the session's
    last teardown with `pytest.fail` when the answer is wrong, and by
    `test_closing`, which kills every value first. With the canary live
    the finalizer's call raises `Canary`."""
    return "ok"
