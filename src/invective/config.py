"""invective's settings: the `[tool.invective]` table of a project's
`pyproject.toml`, which both `invective` and `pytest --mutate` read.

    [tool.invective]
    fail-on-survivors = true   # a survivor no comment accepts fails the run
    max-accepted = 10          # so do more accepted survivors than this
    exclude = ["var/*"]        # left out of the copy the mutants are run in

An unknown key is a refusal, so that a misspelt one is not read as absent.

The project they belong to is found here too: `project_root` walks up from
where a command is started to the directory that `pyproject.toml` is in, so
a command works from anywhere inside the project.
"""

from __future__ import annotations

import os
import tomllib
from typing import NamedTuple

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


def project_root(start: str | None = None) -> str:
    """The top of the project *start* (by default the working directory) is
    in: the nearest directory at or above it that holds a `pyproject.toml`,
    or that is a repository's own directory when that comes first; with
    neither, *start* itself.

    **The nearest marker wins, and a repository's directory ends the walk.**
    The nearest is what nested projects need: a fixture project inside a
    repository is the top only from inside it. Without the stop, a
    `pyproject.toml` that is only a tool's settings in the home directory
    would make the whole home directory the project, copied into the
    temporary directory on every run, with nothing saying so.
    """
    start = os.path.abspath(start if start is not None else os.getcwd())
    here = start
    while True:
        if (os.path.isfile(os.path.join(here, "pyproject.toml"))
                or any(os.path.exists(os.path.join(here, name))
                       for name in _REPOSITORY)):
            return here
        up = os.path.dirname(here)
        if up == here:
            return start
        here = up


def relative_to_root(path: str, root: str) -> str:
    """*path*, as typed where the command was started, given from *root*.

    Raises `ValueError` when it is not inside *root*: a `..` would lead out
    of the copy it is joined to, and on Windows another drive has no
    relative path at all.
    """
    rel = os.path.relpath(os.path.abspath(path), root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise ValueError("%s is outside %s" % (path, root))
    return rel


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
