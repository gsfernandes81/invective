"""What invective keeps in a project between runs: the `.invective/`
directory at the project's top, and in it the killer history.

**The history** (`History`) is the test that last killed each mutant of a
module, keyed by what the mutant is (`mutant_keys`), not by where it is, so
an edit elsewhere in the file leaves the key as it was. A run reads it to
try that test alone first (`mutate.probe_attempt`); nothing read from it is
ever a verdict. A wrong entry, a lost one or a key that matches another
mutant costs time, never a verdict: the test is run alone on the original
before it stands for anything, and a mutant it does not kill is run against
the whole selection.

**Only the main thread writes here**, and every write replaces a whole file
at once (`write_json`), so a reader finds the old file or the new one,
never part of either. A write that fails warns and is dropped: the store
only ever saves time.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable

#: The directory at the project's top that holds what invective keeps.
DIRECTORY = ".invective"
#: The history's file in `DIRECTORY`.
HISTORY = "history.json"
#: The `.gitignore` the directory holds, so that nothing in it is committed
#: and no project has to be told to ignore it.
IGNORE = "# Created by invective automatically.\n*\n"

#: How many times a file a reader holds is tried again before the write is
#: given up: on Windows, a file open for reading cannot be replaced.
RETRIES = 3
#: How long to wait between those tries, in seconds.
RETRY_WAIT = 0.1
#: How old a temporary file left beside the store's files must be, in
#: seconds, to be one a writer killed outright left there and not one being
#: written now.
STALE = 24 * 60 * 60


class Unreadable(Exception):
    """A file of the store that is there and cannot be read as one."""


def directory(root: str) -> str:
    """`DIRECTORY` at *root*, made with its `.gitignore` when it is not
    there. Two processes making it at once are both fine, and a
    `.gitignore` already there is never written over."""
    where = os.path.join(root, DIRECTORY)
    os.makedirs(where, exist_ok=True)
    try:
        with open(os.path.join(where, ".gitignore"), "x", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(IGNORE)
    except FileExistsError:
        pass
    return where


def sweep_temporaries(where: str) -> None:
    """Remove the temporary files in *where* older than `STALE`, which only
    a writer killed between making one and replacing its file leaves."""
    now = time.time()
    try:
        names = os.listdir(where)
    except OSError:
        return
    for name in names:
        if not name.startswith("tmp"):
            continue
        path = os.path.join(where, name)
        with contextlib.suppress(OSError):
            if now - os.path.getmtime(path) > STALE:
                os.remove(path)


def write_json(path: str, data: object,
               say: Callable[[str], object]) -> None:
    """Write *data* to *path* as JSON in one step: written beside it, then
    put in its place, so that a reader never finds half of it. A failure
    is said through *say* and is not raised."""
    folder = os.path.dirname(path)
    sweep_temporaries(folder)
    temp = None
    try:
        handle, temp = tempfile.mkstemp(prefix="tmp", suffix=".json",
                                        dir=folder)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            # invective: accept[equivalent: 1 -> 2] how wide the JSON is indented
            json.dump(data, fh, indent=1)
        for left in range(RETRIES, -1, -1):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if not left:
                    raise
                time.sleep(RETRY_WAIT)
    except OSError as exc:
        say("warning:   %s could not be written, and is left as it was: %s"
            % (path, exc))
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                os.remove(temp)


def read_json(path: str) -> object:
    """The JSON in *path*, or None when there is no such file; `Unreadable`
    when it is there and is not JSON."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise Unreadable(str(exc)) from exc


def mutant_keys(lines: list[str],
                sites: Iterable[tuple[str, ast.AST, str]]) -> list[str]:
    """A key for each of *sites*, `(kind, node, change)` as the engine finds
    them in a module whose lines are *lines*, in their order.

    A key is what the mutant is: its kind and change, its node's text, its
    line's text, and how many sites before it have all four the same. It
    holds no line number or index, so it stays the same when the file is
    edited elsewhere, and two mutants alike in all four are told apart by
    their order.
    """
    seen: dict[tuple, int] = {}
    keys = []
    for kind, node, change in sites:
        try:
            text = ast.unparse(node)
        except ValueError:
            # `ast.unparse` cannot write some f-strings before 3.12.
            text = ast.dump(node)
        what = (kind, change, text,
                lines[node.lineno - 1].strip())           # type: ignore[attr-defined]
        seen[what] = seen.get(what, -1) + 1
        keys.append(json.dumps([*what, seen[what]]))
    return keys


def storable(killer: str) -> bool:
    """Whether *killer* is a test that can be run alone again: a node id,
    and not a module that failed, a run stopped at its time budget, or a run
    that named nothing."""
    return "::" in killer and killer != "TIMEOUT"


class History:
    """The killer each mutant of one module was last killed by, as read
    when the run started (*killers*, key to node id), and what this run's
    outcomes change of it, kept until `save`."""

    def __init__(self, root: str, target: str, killers: dict[str, str],
                 say: Callable[[str], object]) -> None:
        self.root, self.target, self.killers = root, target, killers
        self.say = say
        #: Each key this run decided: its killer, or None to forget it.
        self.changes: dict[str, str | None] = {}

    @classmethod
    def load(cls, root: str, target: str,
             say: Callable[[str], object]) -> History:
        """The history of *target*, a path from *root* with `/` separators;
        an empty one when there is none, or when the file cannot be read,
        which is said through *say*."""
        path = os.path.join(root, DIRECTORY, HISTORY)
        try:
            data = _shaped(read_json(path))
        except Unreadable as exc:
            say("warning:   %s cannot be read, and is ignored: %s"
                % (path, exc))
            data = {}
        return cls(root, target, dict(data.get(target, {})), say)

    def note(self, key: str, ok: bool, killer: str) -> None:
        """The mutant *key* came to a verdict: a kill by *killer* remembers
        it, when it is a test (`storable`), and any other verdict forgets
        whatever was remembered."""
        self.changes[key] = killer if not ok and storable(killer) else None

    def save(self, keys: Iterable[str]) -> None:
        """Write this run's changes over the file as it is now, so that
        another run's changes to another module stand, and forget each key
        of this module not among *keys*, every mutant it has now. Nothing is
        written when nothing changes."""
        path = os.path.join(self.root, DIRECTORY, HISTORY)
        try:
            data = _shaped(read_json(path))
        except Unreadable:
            data = {}
        mine = dict(data.get(self.target, {}))
        for key, killer in self.changes.items():
            if killer is None:
                mine.pop(key, None)
            else:
                mine[key] = killer
        alive = set(keys)
        mine = {key: killer for key, killer in mine.items() if key in alive}
        new = {name: entries for name, entries in data.items()
               if name != self.target}
        if mine:
            new[self.target] = mine
        if new == data:
            return
        try:
            directory(self.root)
        except OSError as exc:
            self.say("warning:   %s could not be made, so the history is not "
                     "kept: %s" % (os.path.join(self.root, DIRECTORY), exc))
            return
        write_json(path, new, self.say)


def _shaped(data: object) -> dict[str, dict[str, str]]:
    """*data*, read from the history's file, when it has the history's
    shape; {} for no file; `Unreadable` for anything else."""
    if data is None:
        return {}
    if not (isinstance(data, dict) and all(
            isinstance(entries, dict)
            and all(isinstance(killer, str) for killer in entries.values())
            for entries in data.values())):
        raise Unreadable("not a history")
    return data
