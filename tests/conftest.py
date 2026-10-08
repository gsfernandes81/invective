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
# tests a copy, and the copy's `src` holds the mutant, so the code under
# test must come from here. The plugin that reports the run's own
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
    # pytest's own settings file with nothing set: pytest stops its search for
    # one here, so what is above the temporary directory is not read, and
    # invective's settings are the defaults.
    "pyproject.toml": "[tool.pytest.ini_options]\n",
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


#: A workspace whose member `m` takes its pytest settings from the top's
#: `pyproject.toml`, where an xfail is strict: the gate's one `RAISE` mutant
#: makes the xfailing test pass, which only a strict xfail fails. From the
#: top, `pythonpath` finds `pkg` in `m`; from `m`, the working directory does.
WORKSPACE = {
    "pyproject.toml": ("[tool.pytest.ini_options]\nxfail_strict = true\n"
                       "pythonpath = ['m']\n"),
    "m/pyproject.toml": "[project]\nname = 'm'\n",
    "m/pkg/__init__.py": "",
    "m/pkg/gate.py": ("def admit(age):\n"
                      "    if age < 18:\n"
                      "        raise ValueError('under age')\n"
                      "    return age\n"),
    "m/tests/test_gate.py": ("import pytest\n"
                             "from pkg import gate\n"
                             "\n"
                             "@pytest.mark.xfail(raises=ValueError)\n"
                             "def test_a_minor_is_refused():\n"
                             "    gate.admit(10)\n"),
}


#: A service with no file pytest takes for a project's, whose tests import
#: `app` from the service's own directory, where they are run.
MARKERLESS = {
    "api/requirements.txt": "pytest\n",
    "api/app/__init__.py": "",
    "api/app/gate.py": ("def admit(age):\n"
                        "    if age > 5:\n"
                        "        raise ValueError('too old')\n"
                        "    return age\n"),
    "api/tests/test_gate.py": ("import pytest\n"
                               "from app import gate\n"
                               "\n"
                               "def test_too_old_is_refused():\n"
                               "    with pytest.raises(ValueError):\n"
                               "        gate.admit(10)\n"),
}


#: A repository whose project `sub` takes its pytest settings from the
#: repository's top, where an xfail is strict: the gate's one `RAISE` mutant
#: makes the xfailing test pass, which only a strict xfail fails. `sub` has
#: a `pyproject.toml` that sets nothing, which is its top.
MONOREPO = {
    "pytest.ini": "[pytest]\nxfail_strict = true\n",
    "sub/pyproject.toml": "[project]\nname = 'sub'\nversion = '0'\n",
    "sub/pkg/__init__.py": "",
    "sub/pkg/gate.py": ("def admit(age):\n"
                        "    if age > 5:\n"
                        "        raise ValueError('too old')\n"
                        "    return age\n"),
    "sub/tests/test_gate.py": ("import pytest\n"
                               "from pkg import gate\n"
                               "\n"
                               "@pytest.mark.xfail(raises=ValueError)\n"
                               "def test_too_old_is_refused():\n"
                               "    gate.admit(10)\n"),
}


def monorepo(path, files):
    """A repository at *path* with *files* committed: the commit's hash."""
    os.makedirs(path)
    git(path, "init", "-q", "-b", "main")
    commit(path, files)
    return git(path, "rev-parse", "HEAD").strip()


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


# The fixture carries its own `pyproject.toml`, which is pytest's settings
# file with nothing set: the nearest marker, and the project's top, is its own
# directory, and pytest's search for settings stops there, whatever lies above
# the temporary directory (a `--basetemp` inside a checkout, or a temporary
# directory under a home directory that holds one). A test whose subject is a
# tree with no settings file calls `no_pytest_settings_above`; the rest rely on
# the one requirement the suite keeps: that no pytest settings file is above
# the temporary directory, which the default under the system temporary
# directory satisfies.
def no_pytest_settings_above(path):
    """Skip the test when pytest, started in *path*, would read a settings
    file from a directory above it: the test is about a tree with none."""
    from invective import config
    found = next(filter(None, map(config._stop,
                                  config._up(os.path.realpath(path)))), None)
    if found is not None and config._holds_settings(found):
        pytest.skip("%s is above the temporary directory and holds pytest "
                    "settings" % found)


@pytest.fixture
def settings_above_the_copy(tmp_path, monkeypatch):
    """A temporary directory below a `pytest.ini` that deselects the test of
    the gate's refusal and a `conftest.py` that leaves a mark, so a run that
    goes by either says so: the gate's `RAISE` mutant survives, or the mark
    is there. Gives the mark's path; the project is for the test to build
    beside it, outside `above`."""
    above = tmp_path / "above"
    mark = tmp_path / "ran.txt"
    write_tree(str(above), {
        "pytest.ini": '[pytest]\naddopts = -k "not test_a_minor_is_refused"\n',
        "conftest.py": ("import pathlib\n"
                        "pathlib.Path(%r).write_text('ran')\n" % str(mark)),
        "tmp/.keep": ""})
    monkeypatch.setattr(tempfile, "tempdir", str(above / "tmp"))
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(above / "tmp"))
    return mark


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
#: pid and its parent's where the test that started the run can read them.
#: The parent is the engine, or on Windows the launcher that a virtual
#: environment's `python.exe` is, which starts the interpreter as its child
#: and is a process of the run as much as the interpreter. On the unmutated
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
    "        fh.write(str(os.getpid()) + ' ' + str(os.getppid()))\n"
    "    time.sleep(60)\n")


@contextlib.contextmanager
def slow_run(tree, tmp_path):
    """`invective run` on *tree* against `SLOW_TEST`, in a process of its own:
    the process, the copy's path from its first line, the pid of the run
    hanging on the mutant, which is the moment a test about a signal sends
    it, and the pid of that run's parent. Whatever the test leaves running is
    stopped on the way out."""
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
        run, parent = (int(pid) for pid in wait_for(pid_file).split())
        yield proc, where, run, parent
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


#: Windows: the access to a process that waiting on it needs.
_SYNCHRONIZE = 0x00100000


def wait_ended(*pids, seconds=30):
    """Return once every process in *pids* has ended, and fail the test if
    one has not within *seconds*.

    On Windows `taskkill /F` only starts a process's end, and the launcher of
    a killed interpreter ends after it, on its own: until then each may still
    hold its working directory, and a directory some process works in cannot
    be renamed. A process's exit code is set before that, so a liveness check
    already reads it as gone; it is waited on instead, which returns once it
    has finished ending. Elsewhere a killed process holds nothing that stops
    a removal, and there is nothing to wait for."""
    if os.name != "nt":
        return
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    end = time.monotonic() + seconds
    for pid in pids:
        handle = kernel.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            # Ended, and nothing holds the process open any more.
            continue
        try:
            left = max(0, int((end - time.monotonic()) * 1000))
            if kernel.WaitForSingleObject(handle, left) != 0:
                pytest.fail("process %d has not ended within %d s"
                            % (pid, seconds))
        finally:
            kernel.CloseHandle(handle)


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
