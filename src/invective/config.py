"""invective's settings: the `[tool.invective]` table of a project's
`pyproject.toml`, which both `invective` and `pytest --mutate` read.

    [tool.invective]
    fail-on-survivors = true   # a survivor no comment accepts fails the run
    max-accepted = 10          # so do more accepted survivors than this
    exclude = ["var/*"]        # left out of the copy the mutants are run in

An unknown key is a refusal, so that a misspelt one is not read as absent.
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
