"""What invective keeps in a project between runs: the `.invective/`
directory at the project's top, and in it the killer history and the
verdict cache.

**The history** (`History`) is the test that last killed each mutant of a
module, keyed by what the mutant is (`mutant_keys`), not by where it is, so
an edit elsewhere in the file leaves the key as it was. A run reads it to
try that test alone first (`mutate.probe_attempt`); nothing read from it is
ever a verdict. A wrong entry, a lost one or a key that matches another
mutant costs time, never a verdict: the test is run alone on the original
before it stands for anything, and a mutant it does not kill is run against
the whole selection.

**The verdict cache** (`Cache`) is what each mutant came to, kept as it
lands under a key of everything the campaign's verdicts can depend on that
invective can see (`campaign_key`), and read back instead of a run by a
later run of the same campaign. Only a verdict the whole selection would
give again is kept (`sound`), and an entry is read only by a run whose
settings could have decided it (`Cache.hit`): a survivor, a kill by a test
that failed, or by a module that would not import.

**Only the main thread writes here**, and every write replaces a whole file
at once (`write_json`), so a reader finds the old file or the new one,
never part of either. A write that fails warns and is dropped: the store
only ever saves time.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import sys
import time
import tomllib
import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import NamedTuple

from pytest import ExitCode

import pytest_invective
from invective.config import _SETTINGS, _start, _up
from invective.tree import MARKER, SKIPPED

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


def write_json(path: str, data: object, say: Callable[[str], object],
               sweep: bool = True) -> bool:
    """Write *data* to *path* as JSON in one step: written beside it, then
    put in its place, so that a reader never finds half of it; whether it
    was. A failure is said through *say* and is not raised. With *sweep*,
    the old temporaries beside it are removed first (`sweep_temporaries`)."""
    folder = os.path.dirname(path)
    if sweep:
        sweep_temporaries(folder)
    # Made as `open` makes a file, with the mode the umask leaves, and not
    # `mkstemp`'s owner-only one: the file is the project's, and another
    # user of a shared checkout reads it.
    temp = os.path.join(folder, "tmp%s.json" % uuid.uuid4().hex)
    try:
        with open(temp, "x", encoding="utf-8", newline="\n") as fh:
            # invective: accept[equivalent: 1 -> 2] how wide the JSON is indented
            json.dump(data, fh, indent=1)
        left = RETRIES
        while True:
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if not left:
                    raise
                left -= 1
                time.sleep(RETRY_WAIT)
    except OSError as exc:
        say("warning:   %s could not be written, and is left as it was: %s"
            % (path, exc))
        return False
    finally:
        with contextlib.suppress(OSError):
            os.remove(temp)
    return True


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


# --------------------------------------------------------------------------
# **The verdict cache.** Each verdict is one file, written as it lands, so
# a run stopped any way at all keeps what it measured, and a run of the same
# campaign after it reads it instead of running the mutant again:
#
#     .invective/cache/<target>/<campaign>/<mutant>.json
#
# each name a hash's first hex digits: of the target's path, of the campaign
# key (`campaign_key`) and of the mutant's text.

#: The cache's directory in `DIRECTORY`.
CACHE = "cache"
#: The version of the cache's key and entries: one that changes leaves every
#: entry kept before it unread.
FORMAT = 1
#: Each attempt whose kill an entry can carry (*origin*), and the setting that
#: turns that attempt on: a kill it decided is read only while it is in use.
ORIGIN_FLAG = {"probe": "history", "coverage": "coverage"}
#: How many campaigns of a target are kept besides the current one, the
#: newest first: a few edits back and forth find theirs, and the rest go.
KEEP = 3
#: The fields of an entry, and nothing else.
FIELDS = frozenset({"campaign", "mutant", "ok", "code", "killer", "origin",
                    "concurrent", "safe"})
#: An entry's file name. A temporary (`tmp*`) is no entry.
_ENTRY = re.compile(r"[0-9a-f]{32}\.json")
#: The variables named like those the key holds that it leaves out:
#: pytest's own, which change from run to run.
_ENV_LEFT = ("PYTEST_CURRENT_TEST", "PYTEST_VERSION")
#: What an import whose file is gone hashes as.
ABSENT = "absent"


def sound(ok: bool, code: int, killer: str) -> bool:
    """Whether a verdict is one the whole selection gives again, run alone
    on the same campaign: a survivor, or a kill by a test that failed
    (`ExitCode.TESTS_FAILED`) or by a module that would not import
    (`ExitCode.INTERRUPTED`), with the test or module named.

    Never a kill by time, by a signal or a crash, by an internal error or a
    usage error, or one that named nothing: each can be the machine's load or
    memory, and read back it would be a kill no run made."""
    if ok:
        return code == 0 and not killer
    return bool(killer) and code in (ExitCode.TESTS_FAILED,
                                     ExitCode.INTERRUPTED)


def _hex(data: str | bytes) -> str:
    return hashlib.sha256(
        data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _digest(path: str) -> str:
    """The SHA-256 of the file *path*, a link read through; of the link's
    own text when it leads nowhere."""
    if os.path.islink(path) and not os.path.exists(path):
        return _hex("link:" + os.readlink(path))
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _toml(value: object) -> object:
    """A TOML date or time, which JSON has no word for."""
    return ["toml", type(value).__name__, value.isoformat()]  # type: ignore[attr-defined]


def _settings_digest(path: str) -> str:
    """The SHA-256 of a `pyproject.toml` as data: what it sets, not how it is
    written, and of `[tool.invective]` only `exclude`, since every other
    setting there changes no verdict or is kept beside the verdict. Its
    bytes when it is not TOML."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return _hex(raw)
    tool = data.get("tool")
    if isinstance(tool, dict) and isinstance(tool.get("invective"), dict):
        ours = tool.pop("invective")
        if "exclude" in ours:
            tool["invective"] = {"exclude": ours["exclude"]}
    return _hex(json.dumps(data, sort_keys=True, default=_toml))


def _under(path: str, top: str) -> bool:
    try:
        rel = os.path.relpath(path, top)
    except ValueError:
        # On Windows, another drive.
        return False
    return rel != os.pardir and not rel.startswith(os.pardir + os.sep)


class Tree(NamedTuple):
    """A copy's files, hashed before its first run, by their paths from *top*
    with `/` separators: the bytes of each (*files*), and each
    `pyproject.toml` read as data too (*data*, `_settings_digest`)."""

    top: str
    files: dict[str, str]
    data: dict[str, str]

    def key(self, path: str) -> str:
        """*path*, an absolute path, as this tree names it: from *top* with
        `/` separators when it is under it, and as it is otherwise."""
        if not _under(path, self.top):
            return path
        return os.path.relpath(path, self.top).replace(os.sep, "/")


def hash_tree(copy: str, top: str) -> Tree:
    """Every file of *copy*, the first copy of a campaign, before any run in
    it, and above it each `conftest.py` and pytest settings file in each
    directory up to *top*, the copy itself or the top of a ref's worktree.

    What a copy leaves out is left out here too (`tree.SKIPPED`, a virtual
    environment), and so is the copy's `tree.MARKER`, which names its
    owner's pid. A link to a directory is hashed by its text: the walk does
    not follow it. An `OSError` is raised: a file that cannot be read is
    one the key cannot hold."""
    hashed = Tree(top, {}, {})

    def add(path: str) -> None:
        hashed.files[hashed.key(path)] = _digest(path)
        if os.path.basename(path) == "pyproject.toml":
            hashed.data[hashed.key(path)] = _settings_digest(path)

    def raised(exc: OSError) -> None:
        raise exc

    for here, dirs, names in os.walk(copy, onerror=raised):
        for name in list(dirs):
            path = os.path.join(here, name)
            if os.path.islink(path):
                hashed.files[hashed.key(path)] = _hex("link:"
                                                      + os.readlink(path))
                dirs.remove(name)
            elif (name in SKIPPED
                  or os.path.isfile(os.path.join(path, "pyvenv.cfg"))):
                dirs.remove(name)
        for name in names:
            if name in SKIPPED or (here == copy and name == MARKER):
                continue
            add(os.path.join(here, name))
    if os.path.normcase(copy) != os.path.normcase(top):
        for here in _up(os.path.dirname(copy), top):
            for name in ("conftest.py",) + _SETTINGS:
                path = os.path.join(here, name)
                if os.path.isfile(path):
                    add(path)
    return hashed


def _import_digest(path: str) -> str:
    """The SHA-256 of a file a run imported, as it is now: of the archive it
    was read from when it is a member of one (a module of a zip on
    `sys.path`), and `ABSENT` when it is gone."""
    here = path
    while not os.path.exists(here):
        up = os.path.dirname(here)
        if up == here:
            return ABSENT
        here = up
    return _digest(here) if os.path.isfile(here) else ABSENT


def _engine_code() -> str:
    """The SHA-256 of every `.py` file of `invective` and `pytest_invective`,
    by their paths: an engine edited without a new version is another
    engine."""
    found = []
    for package in (os.path.dirname(os.path.abspath(__file__)),
                    os.path.dirname(os.path.abspath(pytest_invective.__file__))):
        name = os.path.basename(package)
        for here, _dirs, names in os.walk(package):
            for file in names:
                if file.endswith(".py"):
                    path = os.path.join(here, file)
                    rel = os.path.relpath(path, package).replace(os.sep, "/")
                    found.append((name + "/" + rel, _digest(path)))
    return _hex(json.dumps(sorted(found)))


def _version() -> str:
    try:
        return importlib.metadata.version("invective")
    except importlib.metadata.PackageNotFoundError:
        return ""


def _environment() -> dict[str, str]:
    """The variables that can change what Python or pytest does in a run:
    every `PYTHON*` and `PYTEST_*`, but pytest's own, and `CI`."""
    return {name: value for name, value in os.environ.items()
            if (name == "CI" or name.startswith(("PYTHON", "PYTEST_")))
            and name not in _ENV_LEFT and not name.startswith("PYTEST_XDIST_")}


def _distributions() -> list[list[str]]:
    """Every installed distribution: its name, its version, and its RECORD
    and `direct_url.json` hashed, so that a reinstall from another commit or
    a rebuilt local package counts, at the same version."""
    found = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name") or ""
        found.append([re.sub(r"[-_.]+", "-", name).lower(),
                      dist.metadata.get("Version") or "",
                      *(_hex(text) if text is not None else ""
                        for text in (dist.read_text("RECORD"),
                                     dist.read_text("direct_url.json")))])
    return sorted(found)


def campaign_key(tree: Tree, copy: str, target: str, tests: Sequence[str],
                 options: Sequence[str], selected: Sequence[str],
                 exclude: Sequence[str], imported: Sequence[str]) -> str:
    """The key of a campaign's verdicts: a hash of what they can depend on
    that invective can see, so that a change to any of it starts a fresh
    campaign, which reads nothing kept before it.

    *tree* is the first copy, *copy*, hashed before its first run
    (`hash_tree`); *target* the mutated module's path from *copy* with `/`;
    *tests* and *options* as the campaign runs them; *selected* the tests its
    baseline kept, in their order; *exclude* what the copy leaves out; and
    *imported* the files the baseline imported, each from *copy*'s top with
    `/` or absolute.

    The files it holds, from *tree*, or as they stand when it has not them:
    every file under each top-level directory of *copy* that holds a selected
    test (a selected file at the top, alone); every `conftest.py` of the
    copy, and above it up to the tree's top; every pytest settings file from
    where pytest's search starts (`config._start`) up to the tree's top, a
    `pyproject.toml` as data (`_settings_digest`); the `-c` file of
    *options*; and every file of *imported* but the target. Beside them, the
    engine's version and code, the target's text, the `PYTHON*`, `PYTEST_*`
    and `CI` variables, the interpreter, and every installed distribution.

    An `OSError` is raised: the key cannot be made."""
    chosen: dict[str, str] = {}

    def take(path: str, digest: Callable[[str], str] = _digest) -> None:
        name = tree.key(path)
        found = tree.files.get(name)
        chosen[name] = found if found is not None else digest(path)

    tops = []
    for node in selected:
        path = os.path.normpath(os.path.join(copy, node.partition("::")[0]))
        if not _under(path, copy):
            continue
        first, _sep, rest = os.path.relpath(path, copy).partition(os.sep)
        if rest:
            tops.append(tree.key(os.path.join(copy, first)) + "/")
        else:
            take(path)
    for name, digest in tree.files.items():
        if name.startswith(tuple(tops)) or name.rpartition("/")[2] == (
                "conftest.py"):
            chosen[name] = digest
    # The settings, after the tests' directories: a `pyproject.toml` among
    # them is read as data, so that what it sets decides, not its comments.
    for here in _up(_start(copy, tests), tree.top):
        for name in _SETTINGS:
            path = tree.key(os.path.join(here, name))
            found = tree.data.get(path, tree.files.get(path))
            if found is not None:
                chosen[path] = found
    if "-c" in options[:-1]:
        given = options[list(options).index("-c") + 1]
        take(os.path.normpath(os.path.join(copy, given)))
    mine = os.path.normcase(os.path.normpath(os.path.join(copy, target)))
    for file in imported:
        path = os.path.normpath(os.path.join(copy, file))
        if os.path.normcase(path) != mine:
            take(path, _import_digest)
    parts = {
        "format": FORMAT,
        "invective": {"version": _version(), "code": _engine_code()},
        "target": {"path": target,
                   "sha": tree.files[tree.key(os.path.join(copy, target))]},
        "tests": list(tests), "options": list(options),
        "selected": list(selected), "exclude": list(exclude),
        "files": chosen,
        "env": _environment(),
        "python": [sys.version, sys.platform, sys.implementation.cache_tag,
                   sys.prefix, os.path.realpath(sys.executable)],
        "distributions": _distributions(),
    }
    return _hex(json.dumps(parts, sort_keys=True))


class Entry(NamedTuple):
    """A verdict read back: as the run that kept it landed it, and the
    attempt that decided it (*origin*, `""` for the whole selection)."""

    ok: bool
    code: int
    killer: str
    origin: str


class Cache:
    """The verdicts of one campaign, *key* (`campaign_key`), of the target at
    *target*, a path from *root* with `/`: each read back for a run of
    *count* workers, with or without *confirm*, and each landing of this run
    kept (`put`).

    **An entry is read only by a run that could have decided it** (`hit`):
    with *safe_only* (the unsafe speedups off), only one decided with them
    off; a verdict measured beside other copies' runs, only by a run with
    more than one worker; no kill by a run that confirms its kills at more
    than one worker; and a kill an attempt decided, only while that attempt
    is in use (*origins*, the origins it may read, `""` among them). Neither
    *safe_only* with more than one worker nor with an attempt in use is a run
    the engine makes: `ValueError`.

    Workers read (`get`), and never say anything; only the main thread puts.
    An entry that is there and cannot be read as this campaign's is counted
    (`bad`), for the main thread to say once."""

    def __init__(self, root: str, key: str, count: int, confirm: bool, *,
                 target: str, say: Callable[[str], object],
                 safe_only: bool, origins: Iterable[str]) -> None:
        origins = frozenset(origins) | {""}
        if safe_only and (count > 1 or len(origins) > 1):
            raise ValueError("no run with the unsafe speedups off has %d "
                             "workers or reads %s" % (count, sorted(origins)))
        self.root, self.key, self.count, self.confirm = root, key, count, confirm
        self.say, self.safe_only, self.origins = say, safe_only, origins
        #: Where this target's campaigns are.
        self.campaigns = os.path.join(root, DIRECTORY, CACHE, _hex(target)[:12])
        #: Where this campaign's entries are.
        self.where = os.path.join(self.campaigns, key[:24])
        #: The mutants whose entry is there and could not be read.
        self.bad: set[str] = set()
        self._swept = self._stopped = False

    def _path(self, mutant: str) -> str:
        return os.path.join(self.where, mutant[:32] + ".json")

    def get(self, text: str) -> Entry | None:
        """The verdict kept for the mutant whose text is *text*, when this
        run may read it (`hit`); None otherwise, and when none is kept."""
        mutant = _hex(text)
        path = self._path(mutant)
        try:
            entry = read_json(path)
        except Unreadable:
            # Not there, when something on its way is no directory: a file
            # where `.invective` would be, which no put can get past either.
            if os.path.lexists(path):
                self.bad.add(mutant)
            return None
        if entry is None or not self.hit(entry, mutant):
            return None
        return Entry(entry["ok"], entry["code"], entry["killer"],
                     entry["origin"])

    def hit(self, entry: object, mutant: str) -> bool:
        """Whether this run may read *entry*, read for the mutant whose
        text's hash is *mutant*: of this campaign and this mutant, of a
        shape a run writes (`bad` when not), `sound`, and decided by a run
        this one stands for."""
        if not (isinstance(entry, dict) and set(entry) == FIELDS
                and entry["campaign"] == self.key
                and entry["mutant"] == mutant
                and type(entry["ok"]) is bool and type(entry["code"]) is int
                and isinstance(entry["killer"], str)
                and entry["origin"] in ("", *ORIGIN_FLAG)
                and type(entry["concurrent"]) is bool
                and type(entry["safe"]) is bool
                # No run writes these: with the unsafe speedups off, a run
                # has one worker and no attempt, and a survivor is always the
                # whole selection's, run to its end.
                and not (entry["safe"] and (entry["concurrent"]
                                            or entry["origin"]))
                and not (entry["ok"] and (entry["code"] or entry["killer"]
                                          or entry["origin"]))):
            self.bad.add(mutant)
            return False
        if not sound(entry["ok"], entry["code"], entry["killer"]):
            return False
        if self.safe_only and not entry["safe"]:
            return False
        if entry["concurrent"] and self.count == 1:
            return False
        if self.confirm and self.count > 1 and not entry["ok"]:
            return False
        return entry["origin"] in self.origins

    def put(self, text: str, ok: bool, code: int, killer: str, via: str,
            confirmed: str) -> None:
        """Keep the verdict the mutant whose text is *text* landed with, when
        it is `sound`: decided by the attempt *via*, and confirmed with
        nothing else running as *confirmed* says (`""` when it was not). A
        put that fails is said once, and none is tried after it."""
        if self._stopped or not sound(ok, code, killer):
            return
        mutant = _hex(text)
        entry = {"campaign": self.key, "mutant": mutant, "ok": ok,
                 "code": code, "killer": killer,
                 "origin": via if via in ORIGIN_FLAG else "",
                 "concurrent": self.count > 1 and not confirmed,
                 "safe": self.safe_only}
        try:
            directory(self.root)
            # On every put: another run's `finish` may have removed it.
            os.makedirs(self.where, exist_ok=True)
        except OSError as exc:
            self.say("warning:   %s could not be made, so no verdict is kept: "
                     "%s" % (self.where, exc))
            self._stopped = True
            return
        if not self._swept:
            # Once a run, not once a put, which would list the directory at
            # every landing.
            sweep_temporaries(self.where)
            self._swept = True
        if not write_json(self._path(mutant), entry, self.say, sweep=False):
            self._stopped = True

    def on_record(self) -> int:
        """How many entries this campaign has kept."""
        try:
            return sum(1 for name in os.listdir(self.where)
                       if _ENTRY.fullmatch(name))
        except OSError:
            return 0

    def finish(self) -> None:
        """Mark this campaign the newest of its target's, and remove each of
        the target's others but the `KEEP` newest. Anything that fails is
        left as it is: the cache only saves time."""
        with contextlib.suppress(OSError):
            if os.path.isdir(self.where):
                os.utime(self.where)
        try:
            names = os.listdir(self.campaigns)
        except OSError:
            return
        others = []
        for name in names:
            path = os.path.join(self.campaigns, name)
            with contextlib.suppress(OSError):
                if path != self.where and os.path.isdir(path):
                    others.append((os.path.getmtime(path), path))
        for _mtime, path in sorted(others, reverse=True)[KEEP:]:
            with contextlib.suppress(OSError):
                shutil.rmtree(path)
