"""Starts a thread that sleeps, in the process that loads the conftest.

The thread starts when the conftest is imported (`START_THREAD=conftest`), or when
the first test report reaches a hook (`START_THREAD=report`).
"""

import os
import threading
import time


def _start() -> None:
    """Start a daemon thread named `sleeper` that sleeps for five seconds."""
    threading.Thread(target=time.sleep, args=(5,), name="sleeper", daemon=True).start()


if os.environ.get("START_THREAD") == "conftest":
    _start()


def pytest_runtest_logreport() -> None:
    """With `START_THREAD=report`, start the sleeper unless it is running."""
    if os.environ.get("START_THREAD") == "report" and not any(
        thread.name == "sleeper" for thread in threading.enumerate()
    ):
        _start()
