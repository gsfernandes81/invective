"""Starts a thread that sleeps in the process that loads it, when the
conftest is imported (`START_THREAD=conftest`) or when the first test
report reaches a hook (`START_THREAD=report`)."""

import os
import threading
import time


def _start():
    threading.Thread(target=time.sleep, args=(5,), name="sleeper", daemon=True).start()


if os.environ.get("START_THREAD") == "conftest":
    _start()


def pytest_runtest_logreport(report):
    if os.environ.get("START_THREAD") == "report" and not any(
            thread.name == "sleeper" for thread in threading.enumerate()):
        _start()
