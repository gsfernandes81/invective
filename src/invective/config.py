"""invective's settings: the `[tool.invective]` table of a project's
`pyproject.toml`, which both `invective` and `pytest --mutate` read.

    [tool.invective]
    fail-on-survivors = true   # a survivor no comment accepts fails the run
    max-accepted = 10          # so do more accepted survivors than this
    exclude = ["var/*"]        # left out of the copy the mutants are run in

An unknown key is a refusal, so that a misspelt one is not read as absent.

The project they belong to is found here too: `project_root` walks up from
where a command is started to the nearest directory pytest would take for a
project's own, so a command works from anywhere inside the project.
"""

from __future__ import annotations

import os
import tomllib
from typing import NamedTuple

# pytest's own reader of `.ini` and `.cfg` files, so that one reads here as
# pytest reads it.
import iniconfig

from invective.errors import Refusal


class Config(NamedTuple):
    fail_on_survivors: bool = False
    max_accepted: int | None = None
    exclude: tuple[str, ...] = ()


_KEYS = {"fail-on-survivors": bool, "max-accepted": int, "exclude": list}
_SAID = {bool: "true or false", int: "a whole number", list: "a list"}


#: What marks a repository's own directory, the version-control directories
#: `tree.SKIPPED` leaves out of every copy. Looked for, never asked about:
#: `.git` is a file in a worktree or a submodule, and no program is run.
_REPOSITORY = (".git", ".hg", ".svn")

#: What marks a project's own directory: the files pytest looks for when it
#: settles on a rootdir (its settings files, and `setup.py` when it finds
#: none), and `pyproject.toml`. `pytest.toml` and `.pytest.toml` are read by
#: pytest 9 and later.
_PROJECT = ("pyproject.toml", "pytest.toml", ".pytest.toml", "pytest.ini",
            ".pytest.ini", "tox.ini", "setup.cfg", "setup.py")


def project_root(start: str | None = None) -> str:
    """The top of the project *start* (by default the working directory) is
    in: the nearest directory at or above it, short of the home directory
    when *start* is below that, that holds one of `_PROJECT` or that is a
    repository's own directory; with neither, *start* itself.

    **The nearest marker wins.** The nearest is what nested projects need: a
    fixture project inside a repository is the top only from inside it. And
    every file pytest would recognise is a marker, not `pyproject.toml`
    alone: a project with only a `pytest.ini` under a home directory whose
    `pyproject.toml` holds a tool's settings would otherwise make the whole
    home directory the project, copied into the temporary directory on every
    run, its secrets with it, with nothing saying so. A repository's
    directory ends the walk for the same reason, for a project that has
    neither.

    **Never above the home directory.** The home directory is nobody's
    project but its own: a `pyproject.toml` of a tool's settings there, a
    dotfiles repository, or any marker above it, is the top only for a
    command started in it. Below it, with no marker on the way up, where the
    command was started is the top, as with no marker at all.

    **The top has to hold whatever pytest reads.** Every run is started in
    a copy of the top, so pytest's settings, and a `conftest.py` it loads
    because its rootdir is above the top, are left behind when they are
    above it: each run would go by other settings, and a mutant only they
    kill would survive. `pytest_config_above` names such a file, and the
    commands refuse the run.
    """
    start = os.path.abspath(start if start is not None else os.getcwd())
    # On Windows a drive letter or a name may be spelt in either case. Both
    # sides go through realpath: the start descends from the working
    # directory, which is physical, and `~` may reach the same directory
    # through a link.
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    here = start
    while True:
        if os.path.normcase(os.path.realpath(here)) == home and here != start:
            return start
        if (any(os.path.isfile(os.path.join(here, name)) for name in _PROJECT)
                or any(os.path.exists(os.path.join(here, name))
                       for name in _REPOSITORY)):
            return here
        up = os.path.dirname(here)
        if up == here:
            return start
        here = up


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
    when pytest stops at it and finds nothing set, True when it sets
    something. A file pytest cannot read is True: pytest stops on it too.
    """
    name = os.path.basename(path)
    try:
        if name.endswith(".toml"):
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            if name != "pyproject.toml":
                return bool(data.get("pytest"))
            table = data.get("tool", {}).get("pytest")
            # `[tool.pytest.ini_options]`, even empty, or `[tool.pytest]`'s
            # own keys (pytest 9) make it pytest's.
            if not isinstance(table, dict) or not table:
                return None
            own = [key for key in table if key != "ini_options"]
            return bool(own) or bool(table.get("ini_options"))
        ini = iniconfig.IniConfig(path)
        section = "tool:pytest" if name == "setup.cfg" else "pytest"
        if section in ini.sections:
            return bool(ini.sections[section])
        return False if name in ("pytest.ini", ".pytest.ini") else None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError,
            iniconfig.ParseError):
        return True


def _pytest_reads(start: str) -> str | None:
    """The settings file pytest reads when its search starts at *start*:
    the first of `_SETTINGS` on the way up that is pytest's, or with none
    the nearest `pyproject.toml`, where pytest puts its rootdir instead."""
    here, nearest_pyproject = start, None
    while True:
        for name in _SETTINGS:
            path = os.path.join(here, name)
            if not os.path.isfile(path):
                continue
            if name == "pyproject.toml" and nearest_pyproject is None:
                nearest_pyproject = path
            if _holds_settings(path) is not None:
                return path
        up = os.path.dirname(here)
        if up == here:
            return nearest_pyproject
        here = up


def pytest_file_left_out(root: str, inifile: str | None) -> str | None:
    """The file a pytest whose settings file is *inifile* reads and a copy of
    the project at *root* has not: *inifile* itself when it is outside
    *root* and sets something, or a `conftest.py` between it and *root*,
    which pytest loads because its rootdir is up there; None when there is
    neither, and every run in the copy is the run started here.
    """
    if inifile is None:
        return None
    try:
        relative_to_root(inifile, root)
        return None
    except ValueError:
        pass
    if _holds_settings(inifile):
        return inifile
    # Settings that set nothing still move pytest's rootdir, and with it how
    # far up it loads `conftest.py`: from the settings file's directory down,
    # and only there. The copy's own are copied.
    top = os.path.dirname(os.path.abspath(inifile))
    here = os.path.dirname(os.path.abspath(root))
    try:
        relative_to_root(here, top)
    except ValueError:
        return None
    while True:
        conftest = os.path.join(here, "conftest.py")
        if os.path.isfile(conftest):
            return conftest
        up = os.path.dirname(here)
        if here == top or up == here:
            return None
        here = up


def pytest_reads(root: str, args: tuple[str, ...] | list[str] = ()
                 ) -> str | None:
    """The settings file pytest reads for a run with *args*, as given from
    *root*: `_pytest_reads` from where pytest starts its search, the deepest
    directory every path among them is under, or *root* with none."""
    dirs = []
    for arg in args:
        if arg.startswith("-"):
            continue
        path = os.path.join(root, arg.partition("::")[0])
        if os.path.isdir(path):
            dirs.append(os.path.abspath(path))
        elif os.path.exists(path):
            dirs.append(os.path.dirname(os.path.abspath(path)))
    start = os.path.abspath(root)
    try:
        if dirs:
            common = os.path.commonpath(dirs)
            relative_to_root(common, root)
            start = common
    except ValueError:
        # A path out of the project (on Windows, on another drive) is
        # refused on its own account; pytest's search is the one from here.
        start = os.path.abspath(root)
    return _pytest_reads(start)


def pytest_config_above(root: str, args: tuple[str, ...] | list[str] = ()
                        ) -> str | None:
    """`pytest_file_left_out` for a run of pytest with *args*, as given from
    *root*."""
    return pytest_file_left_out(root, pytest_reads(root, args))


def pytest_settings_option(root: str, args: tuple[str, ...] | list[str] = ()
                           ) -> tuple[str, ...]:
    """The `-c` every run in a copy of *root* is given, naming the settings
    file a run with *args* started here reads, so that each run reads it and
    searches no further. A run started at the top of the copy would search
    past it, above the temporary directory, when the project's file is below
    the top or when pytest only falls back on a `pyproject.toml`. Empty when
    pytest reads no file inside *root*."""
    read = pytest_reads(root, args)
    if read is None:
        return ()
    try:
        return ("-c", relative_to_root(read, root))
    except ValueError:
        # Above the root: `pytest_config_above` has decided the run may go
        # without it, and the copy has no path to it.
        return ()


def outside_refusal(left_out: str, root: str) -> Refusal:
    """The refusal of a run whose pytest reads *left_out*, which a copy of
    the project at *root* does not hold: every mutant's run would go by
    other settings than the run started here, and a mutant only they kill
    would be reported as a survivor."""
    said = ("pytest reads %s, which is above the project's top %s, so the "
            "copy the mutants are run in does not hold it and every run in "
            "there would go by other settings; " % (left_out, root))
    if os.path.basename(left_out) == "conftest.py":
        # Running from its directory would copy whatever is above it too.
        return Refusal(said + "move it into the project, or give the project "
                       "pytest settings of its own, which stop pytest there")
    return Refusal(said + "move them into the project, or run invective from "
                   "%s, so that the copy is made from there"
                   % os.path.dirname(left_out))


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
        # `bool` is an `int` to isinstance, and `true` is no count.
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise Refusal("[tool.invective] %s must be %s, not %r"
                          % (key, _SAID[kind], value))
    exclude = table.get("exclude", [])
    if not all(isinstance(pattern, str) for pattern in exclude):
        raise Refusal("[tool.invective] exclude must be a list of strings")
    return Config(table.get("fail-on-survivors", False),
                  table.get("max-accepted"), tuple(exclude))
