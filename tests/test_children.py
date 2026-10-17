"""The scheduler: it reaps the children it started and no other."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from invective import children


# The xdist worker this suite runs on has execnet's threads; the child
# forked here only sleeps.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_child_the_scheduler_did_not_start_keeps_its_exit_status(tmp_path):
    """A process the parent started elsewhere (a conftest's server, say)
    stays waitable, with its status, after the scheduler has reaped its
    own; reaping with `waitpid(-1)` would take it and leave `0`."""
    other = subprocess.Popen([sys.executable, "-c", "print('up'); raise SystemExit(7)"],
                             stdout=subprocess.PIPE)
    assert other.stdout.read() == b"up\n"
    done = []
    job = children.Job("job", lambda: time.sleep(0.5), str(tmp_path / "out"), None)
    children.run_all([job], 1, done.append)
    assert [d.key for d in done] == ["job"]
    assert other.wait(timeout=10) == 7
