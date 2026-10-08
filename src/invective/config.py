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

So are pytest's own settings, in one decision every command makes about the
tree its mutants are run in (`pytest_settings`): which settings file every
run there is given, searched for as far as the tree's top (the project's
for a copy, the repository's for a ref's tree, `repository_top`), or the
refusal of a run whose settings, or a `conftest.py` pytest loads for them,
the tree does not hold (`pytest_file_left_out`, `outside_refusal`).
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
    kill would survive. `pytest_settings` refuses such a run, naming the
    file.
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
        if any(os.path.isfile(os.path.join(here, name)) for name in _PROJECT):
            return here
        if any(os.path.exists(os.path.join(here, name)) for name in _REPOSITORY):
            return start
        up = os.path.dirname(here)
        if up == here:
            return start
        here = up


def repository_top(path: str) -> str | None:
    """The nearest directory at or above *path* that is a repository's own,
    one of `_REPOSITORY` in it (a worktree holds `.git` as a file); None
    when there is none."""
    here = os.path.abspath(path)
    while True:
        if any(os.path.exists(os.path.join(here, name)) for name in _REPOSITORY):
            return here
        up = os.path.dirname(here)
        if up == here:
            return None
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


def _pytest_reads(start: str, top: str | None = None) -> str | None:
    """The settings file pytest reads when its search starts at *start*:
    the first of `_SETTINGS` on the way up that is pytest's, or with none
    the nearest `pyproject.toml`, where pytest puts its rootdir instead.

    The way up ends at *top*, a directory at or above *start*, when one is
    given, as it does in a tree with nothing above it; *top*'s own files are
    looked at."""
    here, nearest_pyproject = start, None
    # On Windows the same directory may be spelt in either case.
    end = None if top is None else os.path.normcase(top)
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
        if up == here or os.path.normcase(here) == end:
            return nearest_pyproject
        here = up


def pytest_file_left_out(top: str, inifile: str | None) -> str | None:
    """The file a pytest whose settings file is *inifile* reads and a copy of
    the tree at *top* has not: *inifile* itself when it is outside *top* and
    sets something, or a `conftest.py` between it and *top*, which pytest
    loads because its rootdir is up there; None when there is neither, and
    every run in the copy is the run started here.
    """
    if inifile is None:
        return None
    try:
        relative_to_root(inifile, top)
        return None
    except ValueError:
        pass
    if _holds_settings(inifile):
        return inifile
    # Settings that set nothing still move pytest's rootdir, and with it how
    # far up it loads `conftest.py`: from the settings file's directory down,
    # and only there. The copy's own are copied.
    rootdir = os.path.dirname(os.path.abspath(inifile))
    here = os.path.dirname(os.path.abspath(top))
    try:
        relative_to_root(here, rootdir)
    except ValueError:
        return None
    while True:
        conftest = os.path.join(here, "conftest.py")
        if os.path.isfile(conftest):
            return conftest
        up = os.path.dirname(here)
        if here == rootdir or up == here:
            return None
        here = up


def pytest_reads(root: str, args: tuple[str, ...] | list[str],
                 within: str) -> str | None:
    """The settings file pytest finds in the tree whose top is *within*, a
    directory at or above *root*, for a run with *args*, as given from
    *root*: `_pytest_reads` from where pytest starts its search, the deepest
    directory every path among them is under, or *root* with none, as far
    as *within*."""
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
    return _pytest_reads(start, os.path.abspath(within))


def _read_above(top: str, inside: str | None) -> str | None:
    """The settings file pytest reads above *top* when its search found
    *inside* (None for nothing) on its way up to *top*: the first file above
    that stops its search, or with nothing found below, the `pyproject.toml`
    it falls back on; None when it reads *inside*."""
    if inside is not None and _holds_settings(inside) is not None:
        return None
    above = _pytest_reads(os.path.dirname(top))
    if above is not None and (inside is None
                              or _holds_settings(above) is not None):
        return above
    return None


def pytest_config_above(root: str, args: tuple[str, ...] | list[str] = ()
                        ) -> str | None:
    """`pytest_file_left_out` for a run of pytest with *args*, as given from
    *root*, whose search goes on above *root*."""
    return pytest_file_left_out(root, _read_above(
        root, pytest_reads(root, args, within=root)))


def pytest_settings(root: str, where: str,
                    args: tuple[str, ...] | list[str] = (), ref: bool = False,
                    read: str | None = None, given: bool = False
                    ) -> tuple[str, ...]:
    """The `-c` every run in *where* is given, the tree the mutants of the
    project at *root* are run in, at the project's place in it: a copy of
    *root*, or with *ref* a ref's worktree. A `Refusal` naming the file when
    the runs there could not go by the settings the run started here goes
    by, and a mutant only those kill would be reported as a survivor.

    **The tree's top bounds it**: *root* for a copy, and for a ref the top
    of the repository *root* is in, which the worktree's top stands for. A
    file inside the tree is given to every run, from the project's place
    and with `..` when it is above it, so that each run reads it and its
    search ends there; started in the copy, the search would otherwise go on
    above the temporary directory. A file above the tree's top is in no
    tree: refused when it, or a `conftest.py` its rootdir brings in, would
    matter (`pytest_file_left_out`), and let be when not.

    **The file the run started here reads** is *read* when a pytest that
    read it says so (`pytest --mutate`'s own; *given* when it was named with
    `-c`, which decides the run even when it sets nothing). Otherwise it is
    found as pytest finds it for a run with *args*, as given from the
    project: for a copy in the files as they stand, so that a file the copy
    leaves out is refused and not replaced by another one the copy holds;
    for a ref in the ref's own tree, as the ref's own pytest reads it; above
    the tree's top, in the files as they stand around it.

    With no file to give, every run's search goes on above *where*, and a
    file there that would matter refuses the run too.
    """
    top = (repository_top(root) if ref else None) or root
    option = outside = None
    if read is None:
        tree = where if ref else root
        inside = pytest_reads(tree, args, within=os.path.normpath(
            os.path.join(tree, os.path.relpath(top, root))))
        if inside is not None:
            option = os.path.relpath(inside, tree)
        outside = _read_above(top, inside)
    else:
        try:
            option = os.path.relpath(
                os.path.join(top, relative_to_root(read, top)), root)
        except ValueError:
            outside = read
    if outside is not None:
        if given:
            whose = (("the repository", "ref's tree", "ref's") if ref else
                     ("the project", "copy", "project's"))
            raise Refusal("pytest's settings file %s, given with -c, is "
                          "outside %s at %s: no %s holds it, and every "
                          "mutant's run would read the %s own settings "
                          "instead" % (outside, whose[0], top, whose[1],
                                       whose[2]))
        left_out = pytest_file_left_out(top, outside)
        if left_out:
            raise outside_refusal(left_out, top, ref)
    if option is None:
        # With no file to name, nothing in the tree stops a run's search: it
        # goes on above the copy, into the temporary directory's ancestors,
        # where a settings file, or a `conftest.py` its rootdir brings in,
        # would decide every run, the baseline's too. pytest's search starts
        # from its working directory as the kernel spells it, so through a
        # temporary directory reached by a link it climbs the target's.
        left_out = pytest_config_above(os.path.realpath(where))
        if left_out:
            raise Refusal(
                "pytest reads %s, above the copy the mutants are run in at "
                "%s, so every run in there would go by it; give the project "
                "pytest settings of its own (an empty "
                "[tool.pytest.ini_options] table in pyproject.toml is "
                "enough), or a temporary directory with nothing above it"
                % (left_out, where))
        return ()
    # A file the tree lacks fails every run in pytest's config load: a
    # baseline that reads as red when no test failed.
    if not os.path.isfile(os.path.join(where, option)):
        raise Refusal(
            "pytest read its settings from %s, which the tree the mutants "
            "are made in does not hold (excluded, ignored by git, or not in "
            "the ref), so no run in there can go by the settings this one "
            "did" % option)
    return ("-c", option)


def outside_refusal(left_out: str, root: str, ref: bool = False) -> Refusal:
    """The refusal of a run whose pytest reads *left_out*, which the tree the
    mutants are run in does not hold: a copy of the project at *root*, or
    with *ref* a ref's tree, *root* then being the top of its repository.
    Every mutant's run would go by other settings than the run started
    here, and a mutant only they kill would be reported as a survivor."""
    above, tree, into = (
        ("the top of the repository", "the ref's tree", "the repository")
        if ref else ("the project's top", "the copy", "the project"))
    said = ("pytest reads %s, which is above %s %s, so %s the mutants are run "
            "in does not hold it and every run in there would go by other "
            "settings; " % (left_out, above, root, tree))
    if os.path.basename(left_out) == "conftest.py":
        # Running from its directory would copy whatever is above it too.
        return Refusal(said + "move it into %s, or give the project pytest "
                       "settings of its own, which stop pytest there" % into)
    there = os.path.normcase(os.path.realpath(os.path.dirname(left_out)))
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    # The home directory is nobody's project, and a copy made from it, or
    # from a directory above it, would carry everything in it. A root of the
    # filesystem is never offered either: on Windows a drive's root need not
    # hold the home directory (`D:\` while it is on `C:`), and a copy of it
    # is a copy of the whole drive. A ref's tree is its repository's from
    # wherever invective is run, so running it elsewhere changes nothing.
    if (ref or os.path.dirname(there) == there
            or _inside(home, there) is not None):
        return Refusal(said + "move them into %s" % into)
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
