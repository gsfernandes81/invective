"""A small git repository to mutate, built fresh for each test that asks."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time

import pytest

# **This checkout's `src`, ahead of any installed copy, and put there by a
# conftest rather than by `pythonpath`.** A run of invective against itself
# tests a worktree, and the worktree's `src` holds the mutant, so the code
# under test must come from here. The plugin that reports the run's own
# verdict must not: pytest 8.4 and later apply `pythonpath` before loading
# plugins, which would make a mutant of the plugin the judge of its own run.
# Every pytest imports a conftest after its plugins.
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")
sys.path.insert(0, SRC)


@pytest.fixture(autouse=True)
def own_temp(tmp_path, monkeypatch):
    """Every test's temporary directory is its own, for the processes it
    starts too, and the real one is never looked at.

    Every copy begins by removing the copies in there whose owner is dead.
    The suite's workers each make copies, whose owner is alive; a test that
    kills a run leaves a dead one, which a sibling could reap before the test
    looks; and in a run of invective on its own code, a mutant of the reaper
    that reads a live owner as dead would remove the campaign's own copy, the
    one that holds the mutant, from inside the campaign's tests.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(tmp_path))


#: The fixture repository, as path -> text. Its tests live INSIDE the package,
#: which is the layout where a sweep has to tell the suite's own files from
#: the modules it is there to mutate.
FILES = {
    "pkg/__init__.py": "",
    # Line 2 is refused by a test; the `and` on line 4 is checked by nothing.
    "pkg/gate.py": (
        "def admit(age, member):\n"
        "    if age < 18:\n"
        "        raise ValueError('under age')\n"
        "    if member and age >= 65:\n"
        "        return 'senior'\n"
        "    return 'adult'\n"),
    "pkg/idle.py": (
        "def never():\n"
        "    raise RuntimeError('nothing imports this module')\n"),
    "pkg/sub/__init__.py": "",
    "pkg/sub/deep.py": (
        "def deep(x):\n"
        "    if x is None:\n"
        "        raise ValueError('nothing given')\n"
        "    return x\n"),
    "pkg/tests_extra/aid.py": "def aid():\n    return 1\n",
    # Neither of these two is a module to mutate.
    "pkg/notes.txt": "not python\n",
    "pkg/sub/test_inline.py": "def test_inline():\n    pass\n",
    "pkg/tests/conftest.py": (
        "import os\n"
        "import sys\n"
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..',"
        " 'loose'))\n"),
    # Imports `gate` and `tool`, and is still not a test file.
    "pkg/tests/helpers.py": (
        "import tool\n"
        "from pkg import gate\n"
        "\n"
        "def helper():\n"
        "    raise RuntimeError('harness')\n"),
    "pkg/tests/test_gate.py": (
        "import pytest\n"
        "from pkg import gate\n"
        "\n"
        "def test_a_minor_is_refused():\n"
        "    with pytest.raises(ValueError):\n"
        "        gate.admit(10, False)\n"
        "\n"
        "def test_an_adult_is_admitted():\n"
        "    assert gate.admit(30, False) == 'adult'\n"),
    "pkg/tests/test_deep_things.py": (
        "import pytest\n"
        "from pkg.sub import deep\n"
        "\n"
        "def test_nothing_is_refused():\n"
        "    with pytest.raises(ValueError):\n"
        "        deep.deep(None)\n"),
    # The refusal in `tool` is checked by nothing here.
    "pkg/tests/test_tool.py": (
        "import tool\n"
        "\n"
        "def test_a_count_comes_back():\n"
        "    assert tool.tool(3) == 3\n"),
    "loose/tool.py": (
        "def tool(n):\n"
        "    if n < 0:\n"
        "        raise ValueError('negative')\n"
        "    return n\n"),
}


def write_tree(root, files):
    for rel, text in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


def git(root, *args):
    done = subprocess.run(["git", "-C", root, *args], capture_output=True,
                          text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def commit(root, files=None):
    """Write *files* into the repository at *root*, and commit everything."""
    write_tree(root, files or {})
    git(root, "add", "-A")
    git(root, "-c", "user.name=fixture", "-c",
        "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "fixture")


# The tests that reach `main` or the plugin from here rely on no
# `pyproject.toml` or `.git` above the temporary directory, as the project's
# top is the nearest: a `TMPDIR` inside a checkout makes every such test copy
# that checkout.
@pytest.fixture
def tree(tmp_path):
    """The fixture files on disk, with no repository around them."""
    root = os.path.realpath(tmp_path / "tree")
    write_tree(root, FILES)
    return root


@pytest.fixture
def repo(tree, monkeypatch):
    """The fixture files committed to a repository, and the cwd inside it."""
    git(tree, "init", "-q", "-b", "main")
    commit(tree)
    monkeypatch.chdir(tree)
    return tree


#: A test that hangs on the gate's one `RAISE` mutant, after writing its own
#: pid where the test that started the run can read it. On the unmutated
#: tree the refusal fires and it returns at once, so the baseline is green
#: and quick; on the mutant it sleeps, so a signal sent once the pid is there
#: lands mid-campaign, on the first mutant's run.
SLOW_TEST = (
    "import os\n"
    "import time\n"
    "from pkg import gate\n"
    "\n"
    "def test_slow():\n"
    "    try:\n"
    "        gate.admit(10, False)\n"
    "    except ValueError:\n"
    "        return\n"
    "    with open(%r, 'w') as fh:\n"
    "        fh.write(str(os.getpid()))\n"
    "    time.sleep(60)\n")


@contextlib.contextmanager
def slow_run(tree, tmp_path):
    """`invective run` on *tree* against `SLOW_TEST`, in a process of its own:
    the process, the copy's path from its first line, and the pid of the run
    hanging on the mutant, which is the moment a test about a signal sends
    it. Whatever the test leaves running is stopped on the way out."""
    pid_file = str(tmp_path / "run.pid")
    write_tree(tree, {"pkg/tests/test_slow.py": SLOW_TEST % pid_file})
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "invective", "run",
         "--target", os.path.join("pkg", "gate.py"),
         "--tests", "pkg/tests/test_slow.py", "--only", "RAISE"],
        cwd=tree, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": SRC})
    run = None
    try:
        line = proc.stdout.readline()
        # An engine that could not start has ended, and said why on stderr.
        assert line.startswith("copy:"), (line, proc.communicate(timeout=30))
        where = line.split(None, 1)[1].strip()
        run = int(wait_for(pid_file))
        yield proc, where, run
    finally:
        if proc.poll() is None:
            proc.kill()
        if run is not None:
            stop_group(run)
        proc.communicate()


def wait_for(path, seconds=30):
    """The text of *path*, once it is there and holds some."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            if text:
                return text
        time.sleep(0.05)
    pytest.fail("%s never appeared" % path)


def stop_group(pid):
    """Stop the process *pid* and everything in its group, as the engine's
    `_stop` does. `/F`, because without it `taskkill` only asks, and a run
    still winding down holds the copy as its working directory."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
