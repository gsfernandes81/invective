"""invective's settings: the `[tool.invective]` table of a project's
`pyproject.toml`, which both `invective` and `pytest --mutate` read.

    [tool.invective]
    fail-on-survivors = true   # a survivor no comment accepts fails the run
    max-accepted = 10          # so do more accepted survivors than this
    exclude = ["var/*"]        # left out of the copy the mutants are run in
    workers = 1                # mutants run at once; "auto" for one per CPU

An unknown key is a refusal, so that a misspelt one is not read as absent.

The project they belong to is found here too: `project_root` walks up from
where a command is started to the nearest directory pytest would take for a
project's own, so a command works from anywhere inside the project.

So is whether pytest's own settings reach the mutants' runs
(`check_pytest_settings`): every run goes by what pytest's search finds in
the tree the mutants are run in, and a run whose project reads settings other
than `markers`, or a `conftest.py`, from above its top, where no such tree
reaches, is refused.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Iterator, Sequence
from itertools import islice
from typing import NamedTuple

# pytest's own reader of `.ini` and `.cfg` files, so that one reads here as
# pytest reads it.
import iniconfig

from invective.errors import Refusal


class Config(NamedTuple):
    fail_on_survivors: bool = False
    max_accepted: int | None = None
    exclude: tuple[str, ...] = ()
    workers: int | str = 1


#: Each key and its type; `workers`, a count or "auto", has a check of its
#: own (`workers`).
_KEYS = {"fail-on-survivors": bool, "max-accepted": int, "exclude": list,
         "workers": (int, str)}
_SAID = {bool: "true or false", int: "a whole number", list: "a list"}

#: The most copies "auto" makes: each is a whole copy of the project, and
#: past this the mutants' texts, made one at a time, are what a campaign
#: waits for.
AUTO_MOST = 8

#: The CPU quota of this process's cgroup (v2): `max` or a quota, then the
#: period it is counted over, both in microseconds.
_CPU_MAX = "/sys/fs/cgroup/cpu.max"


def workers(value: int | str, name: str = "workers",
            most: int | None = None) -> int:
    """How many mutants *value* runs at once: a whole number above 0, as a
    number or as its digits, the way a command line gives it; or "auto",
    one for each CPU this process may use (`_cpus`), at most `AUTO_MOST`
    and at most *most*. Anything else is a refusal naming *name*."""
    if value == "auto":
        return max(1, min(_cpus(), AUTO_MOST,
                          AUTO_MOST if most is None else most))
    count = value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        count = int(value)
    # `bool` is an `int` to isinstance, and `true` is no count.
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return count
    raise Refusal('%s must be a whole number above 0 or "auto", not %r'
                  % (name, value))


def _cpus() -> int:
    """The CPUs this process may run on, and no more than its cgroup's
    quota allows: a container given two CPUs of a host's sixty-four sees all
    sixty-four, and a copy for each would only queue for the two."""
    if hasattr(os, "process_cpu_count"):
        count = os.process_cpu_count()
    elif hasattr(os, "sched_getaffinity"):
        count = len(os.sched_getaffinity(0))
    else:
        count = None
    count = count or os.cpu_count() or 1
    try:
        with open(_CPU_MAX, encoding="ascii") as fh:
            quota, period = fh.read().split()
        if quota != "max":
            count = min(count, max(1, int(quota) // int(period)))
    except (OSError, ValueError):
        # No cgroup v2 file to read: Windows, macOS, or a cgroup v1 host.
        pass
    return count


#: What marks a repository's own directory, the version-control directories
#: `tree.SKIPPED` leaves out of every copy, where `project_root`'s walk ends.
#: Looked for, never asked about: `.git` is a file in a worktree or a
#: submodule, and no program is run.
_REPOSITORY = (".git", ".hg", ".svn")

#: What marks a project's own directory: the files pytest looks for when it
#: settles on a rootdir (its settings files, and `setup.py` when it finds
#: none), and `pyproject.toml`. `pytest.toml` and `.pytest.toml` are read by
#: pytest 9 and later.
_PROJECT = ("pyproject.toml", "pytest.toml", ".pytest.toml", "pytest.ini",
            ".pytest.ini", "tox.ini", "setup.cfg", "setup.py")


def project_root(start: str | None = None) -> str:
    """The top of the project *start* (by default the working directory) is
    in: the nearest directory at or above it that holds one of `_PROJECT`;
    the walk ends at a repository's own directory and short of the home
    directory, and with no marker by then, *start* itself.

    **The nearest marker wins.** The nearest is what nested projects need: a
    fixture project inside a repository is the top only from inside it. And
    every file pytest would recognise is a marker, not `pyproject.toml`
    alone: a project with only a `pytest.ini` under a home directory whose
    `pyproject.toml` holds a tool's settings would otherwise make the whole
    home directory the project, copied into the temporary directory on every
    run, its secrets with it, with nothing saying so. A repository's
    directory ends the walk for the same reason, and is no project's top by
    itself: a project with no marker is the directory the command was
    started in, as with no repository at all, so a marker-less project
    inside a repository is measured from its own top, where its tests
    import it, and is never copied together with everything beside it.

    **Never above the home directory.** The home directory is nobody's
    project but its own: a `pyproject.toml` of a tool's settings there, a
    dotfiles repository, or any marker above it, is the top only for a
    command started in it. Below it, with no marker on the way up, where the
    command was started is the top, as with no marker at all.

    **The top has to hold whatever pytest reads.** Every run is started in
    a copy of the top, so pytest's settings, and a `conftest.py` it loads
    because its rootdir is above the top, are left behind when they are
    above it: each run would go by other settings, and a mutant only they
    kill would survive. `check_pytest_settings` refuses such a run, naming
    the file.
    """
    start = os.path.abspath(start if start is not None else os.getcwd())
    # On Windows a drive letter or a name may be spelt in either case. Both
    # sides go through realpath: the start descends from the working
    # directory, which is physical, and `~` may reach the same directory
    # through a link.
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    for here in _up(start):
        if os.path.normcase(os.path.realpath(here)) == home and here != start:
            return start
        if any(os.path.isfile(os.path.join(here, name)) for name in _PROJECT):
            return here
        if any(os.path.exists(os.path.join(here, name)) for name in _REPOSITORY):
            return start
    return start


def repository_top(path: str) -> str | None:
    """The nearest directory at or above *path* that is a repository's own,
    one of `_REPOSITORY` in it (a worktree holds `.git` as a file); None
    when there is none."""
    return next((here for here in _up(os.path.abspath(path))
                 if any(os.path.exists(os.path.join(here, name))
                        for name in _REPOSITORY)), None)


def _inside(path: str, root: str) -> str | None:
    """*path*, an absolute path, given from *root*; None when it is not
    inside it."""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel


def relative_to_root(path: str, root: str) -> str:
    """*path*, as typed where the command was started, given from *root*.

    A link inside the project is kept as typed, wherever it leads: the
    project's tests reach the file through it. A path reached through a link
    to the project, as every absolute path is from a working directory
    entered through one, is the project's own file, and is given from the
    root once both are resolved.

    Raises `ValueError` when it is not inside *root* either way: a `..` would
    lead out of the copy it is joined to, and on Windows another drive has
    no relative path at all.
    """
    rel = _inside(os.path.abspath(path), root)
    if rel is None:
        rel = _inside(os.path.realpath(path), os.path.realpath(root))
    if rel is None:
        raise ValueError("%s is outside %s" % (path, root))
    return rel


#: The files pytest reads its settings from, in the order it looks for them
#: in each directory on its way up.
_SETTINGS = ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini",
             "pyproject.toml", "tox.ini", "setup.cfg")


def _holds_settings(path: str) -> bool | None:
    """Whether pytest finds settings in *path*, one of `_SETTINGS`: None when
    it is no settings file to pytest and the search goes on past it, False
    when pytest stops at it and finds nothing set that can change what a run
    says, True when it does. A file pytest cannot read is True: pytest stops
    on it too.
    """
    name = os.path.basename(path)
    try:
        if name.endswith(".toml"):
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            if name != "pyproject.toml":
                return _sets(data.get("pytest"))
            table = data.get("tool", {}).get("pytest")
            # `[tool.pytest.ini_options]`, even empty, or `[tool.pytest]`'s
            # own keys (pytest 9) make it pytest's.
            if not isinstance(table, dict) or not table:
                return None
            own = {key: value for key, value in table.items()
                   if key != "ini_options"}
            return _sets(own) or _sets(table.get("ini_options"))
        ini = iniconfig.IniConfig(path)
        section = "tool:pytest" if name == "setup.cfg" else "pytest"
        if section in ini.sections:
            return _sets(dict(ini.sections[section]))
        return False if name in ("pytest.ini", ".pytest.ini") else None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError,
            iniconfig.ParseError):
        return True


def _sets(table: object) -> bool:
    """Whether a table of pytest's settings sets anything but `markers`.

    `markers` only registers marks' names. Without it an unregistered mark
    warns, and under strict markers, or with warnings made errors, it fails
    collection, so every run, the baseline first, is red and refused: it
    never changes a verdict silently. Every other setting can."""
    if isinstance(table, dict):
        return bool(table.keys() - {"markers"})
    return bool(table)


def _up(start: str, end: str | None = None) -> Iterator[str]:
    """The directories from *start* up to *end*, both included, or to the
    root of the filesystem."""
    here = start
    while True:
        yield here
        up = os.path.dirname(here)
        # On Windows the same directory may be spelt in either case.
        if up == here or (end is not None and
                          os.path.normcase(here) == os.path.normcase(end)):
            return
        here = up


def _stop(here: str) -> str | None:
    """The file in the directory *here* at which pytest's search for its
    settings ends; None when the search goes on past it."""
    paths = (os.path.join(here, name) for name in _SETTINGS)
    return next((path for path in paths if os.path.isfile(path)
                 and _holds_settings(path) is not None), None)


def _start(where: str, args: Sequence[str]) -> str:
    """Where pytest's search for settings starts for a run in *where* with
    *args*: the deepest directory every path among them is under, or with
    none, or one that leads out of *where*, *where* itself."""
    dirs = []
    for arg in args:
        path = os.path.abspath(os.path.join(where, arg.partition("::")[0]))
        if not arg.startswith("-") and os.path.exists(path):
            dirs.append(path if os.path.isdir(path) else os.path.dirname(path))
    try:
        common = os.path.commonpath(dirs) if dirs else where
    except ValueError:
        # On Windows, a path on another drive.
        return where
    return common if _inside(common, where) is not None else where


def _read_above(top: str, fallback: bool) -> str | None:
    """What pytest reads above the directory *top* that can change what a
    run says, once its search has found nothing below *top* that ends it.

    First, the settings file that ends the search above *top*, when it sets
    anything but `markers`. Failing that, the first `conftest.py` on the way
    up to pytest's rootdir, which is the directory of the file that ends the
    search; when no file does, that of the nearest `pyproject.toml`, unless
    *fallback* says the tree holds a nearer one. None when there is neither."""
    conftest = loaded = None
    for here in islice(_up(top), 1, None):
        if conftest is None and os.path.isfile(os.path.join(here,
                                                            "conftest.py")):
            conftest = os.path.join(here, "conftest.py")
        stop = _stop(here)
        if stop is not None:
            return stop if _holds_settings(stop) else conftest
        if not fallback and os.path.isfile(os.path.join(here,
                                                        "pyproject.toml")):
            fallback, loaded = True, conftest
    return loaded


def check_pytest_settings(root: str, where: str, args: Sequence[str] = (),
                          ref: bool = False) -> None:
    """Refuse, naming the file, a campaign whose runs would go by other
    pytest settings than the project's own pytest does, so that a mutant
    only those settings kill would be reported as a survivor.

    Each run is pytest's own, started with *args* at the project's place
    in *where*, the tree the mutants of the project at *root* are run in: a
    copy of *root*, or with *ref* a ref's worktree, whose top stands for the
    top of the repository *root* is in. invective names no settings file to
    it, and its search for one is pytest's: when a file in the tree ends
    it, nothing outside the tree is read. When none does, the project's own
    pytest goes on above the project's top (the repository's, for a ref),
    where no tree reaches: a settings file there that sets something, or a
    `conftest.py` pytest loads because its rootdir is there, refuses.
    """
    top = (repository_top(root) if ref else None) or root
    tree = os.path.normpath(os.path.join(where, os.path.relpath(top, root)))
    fallback = False
    for here in _up(_start(where, args), tree):
        if _stop(here) is not None:
            return
        fallback = fallback or os.path.isfile(os.path.join(here,
                                                           "pyproject.toml"))
    left = _read_above(top, fallback)
    if left is None:
        return
    above, held, into = (
        ("the top of the repository", "the ref's tree", "the repository")
        if ref else ("the project's top", "the copy", "the project"))
    said = ("pytest reads %s, which is above %s %s, so %s the mutants are run "
            "in does not hold it and every run in there would go by other "
            "settings; " % (left, above, top, held))
    if os.path.basename(left) == "conftest.py":
        raise Refusal(said + "move it into %s, or give the project pytest "
                      "settings of its own, which stop pytest there" % into)
    raise Refusal(said + "move them into %s" % into)


def load(root: str) -> Config:
    """The settings in *root*'s `pyproject.toml`, or the defaults without one."""
    path = os.path.join(root, "pyproject.toml")
    try:
        with open(path, "rb") as fh:
            table = tomllib.load(fh).get("tool", {}).get("invective", {})
    except FileNotFoundError:
        return Config()
    except tomllib.TOMLDecodeError as exc:
        raise Refusal("%s is not valid TOML: %s" % (path, exc)) from exc
    for key, value in table.items():
        kind = _KEYS.get(key)
        if kind is None:
            raise Refusal("[tool.invective] has no setting %r; it has %s"
                          % (key, ", ".join(sorted(_KEYS))))
        if key == "workers":
            workers(value, "[tool.invective] workers")
            continue
        # `bool` is an `int` to isinstance, and `true` is no count.
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise Refusal("[tool.invective] %s must be %s, not %r"
                          % (key, _SAID[kind], value))
    exclude = table.get("exclude", [])
    if not all(isinstance(pattern, str) for pattern in exclude):
        raise Refusal("[tool.invective] exclude must be a list of strings")
    return Config(table.get("fail-on-survivors", False),
                  table.get("max-accepted"), tuple(exclude),
                  table.get("workers", 1))
