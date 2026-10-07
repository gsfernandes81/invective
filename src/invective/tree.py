"""Where a campaign's mutants are written: a copy of the project as it stands,
or of one of its commits.

Either is a directory of its own, made before the first run and removed after
the last, so the files a person is working on never hold a mutant, and a run
that is killed leaves nothing behind in them. Each is a context manager that
gives the path of the copy's top level.

**A copy carries the pid of the process it belongs to**, in `MARKER` at the
top of its directory (for a ref, the directory the worktree is checked out
into), and each context manager begins by removing the copies whose owner is
dead. The removal at the end runs on an interrupt and a SIGTERM, which the
engine turns into one, and on nothing else: a run killed outright leaves its
copy, a whole working tree with whatever untracked files hold, and the marker
is how the next invective to start knows it may be removed.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import shutil
import subprocess
import tempfile

from invective.errors import Refusal

#: Directories never copied: version control, caches, and the tools' own
#: working space. A virtual environment is left out as well, whatever its
#: name, by the `pyvenv.cfg` every one of them holds.
SKIPPED = frozenset({".git", ".hg", ".svn", ".tox", ".nox", "__pycache__",
                     ".pytest_cache", ".mypy_cache", ".ruff_cache",
                     "node_modules"})

#: The file at the top of a copy's directory that names its owner:
#: `{"pid": ..., "root": ...}`, the process the copy belongs to and the
#: project it is a copy of. `root` is read by nothing here; it is for a
#: person who finds a copy and wants to know whose it was.
MARKER = ".invective-owner"


@contextlib.contextmanager
def working_tree(root: str, exclude: tuple[str, ...] = ()):
    """A copy of *root* as it stands, uncommitted edits and untracked files
    included. *exclude* holds glob patterns, matched against paths relative to
    *root* with `/` between their parts, of anything else to leave out."""
    reap()
    root = os.path.abspath(root)
    # Between here and the marker, a kill leaves an empty, unmarked
    # `invective-*` directory that no reaper touches. The window is
    # microseconds, a rule for empty unmarked directories would need an age,
    # and an empty directory holds no secret: left.
    where = tempfile.mkdtemp(prefix="invective-")

    def ignore(directory, names):
        left = set()
        for name in names:
            full = os.path.join(directory, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if (os.path.isdir(full) and not os.path.islink(full)
                    and (name in SKIPPED
                         or os.path.isfile(os.path.join(full, "pyvenv.cfg"))
                         # The copy itself, when the temporary directory is
                         # inside the project: copying it into itself would
                         # never end.
                         or os.path.samefile(full, where))):
                left.add(name)
            elif directory == root and name == MARKER:
                # A marker of the project's own -- the leaked copy of
                # something, being measured -- would be copied over the live
                # one, and a reaper would read a stale pid there.
                left.add(name)
            elif any(fnmatch.fnmatchcase(rel, pattern) for pattern in exclude):
                left.add(name)
        return left

    try:
        # Before the copy is filled, so a run killed halfway through the
        # copying still leaves a marked directory.
        _mark(where, root)
        try:
            shutil.copytree(root, where, symlinks=True, ignore=ignore,
                            dirs_exist_ok=True)
        except (shutil.Error, OSError) as exc:
            raise Refusal("could not copy %s: %s" % (root, exc)) from exc
        yield where
    finally:
        _remove(where)


@contextlib.contextmanager
def git_ref(root: str, ref: str):
    """The tree of commit *ref* of the git repository *root* is in, as a
    detached git worktree; the copy's top level is the directory there that
    corresponds to *root*. The only part of invective that needs git."""
    reap()
    # The worktree is a subdirectory of the copy's directory, which is marked
    # before the checkout: git wants the directory it checks out into empty,
    # and a kill while it fills a large tree, which takes seconds, must leave
    # a copy the next reaper removes. The unmarked window is
    # `working_tree`'s.
    where = tempfile.mkdtemp(prefix="invective-")
    tree = os.path.join(where, "tree")
    try:
        _mark(where, root)
        prefix = _git(root, "rev-parse", "--show-prefix").strip()
        _git(root, "worktree", "add", "--detach", tree, ref)
    except BaseException:
        # A Refusal is the ordinary way here; the rest is a SIGTERM or ^C
        # during the checkout, which leaves a worktree entry that git keeps
        # as locked.
        _discard(root, where)
        raise
    try:
        yield os.path.join(tree, *prefix.split("/")) if prefix else tree
    finally:
        _discard(root, where)


def _remove(where: str) -> None:
    """Remove the copy *where*, raising nothing.

    Renamed first, atomically, to `invective-dead-<name>`, as `reap()` does:
    removing a large tree takes seconds and may delete the marker first, and
    a kill during it then leaves a name the next reaper removes
    unconditionally, not an unmarked directory it never touches. A rename
    that fails -- on Windows a file the stopped run holds open is enough --
    leaves the removal to be done in place."""
    dead = os.path.join(os.path.dirname(where), "invective-dead-"
                        + os.path.basename(where)[len("invective-"):])
    try:
        os.rename(where, dead)
    except OSError:
        dead = where
    shutil.rmtree(dead, ignore_errors=True)


def _discard(root: str, where: str) -> None:
    """Remove the copy *where* that `git_ref` made of *root*, and the entry
    git keeps of its worktree, locked or not. Says nothing and raises
    nothing: it runs on the way out of a run whose own outcome is the one to
    report, and where there may be no git."""
    _remove(where)
    # invective: accept[equivalent: True -> False] git says nothing here a person needs
    quietly = {"capture_output": True, "text": True}
    try:
        # With the directory gone, `remove` on the registered path clears the
        # entry, a locked one only with `--force` given twice; `prune` drops
        # the entries of earlier runs' copies that a reaper removed.
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force",
                        "--force", os.path.join(where, "tree")], **quietly)
        subprocess.run(["git", "-C", root, "worktree", "prune"], **quietly)
    except FileNotFoundError:
        pass


def _git(root: str, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", root, *args], capture_output=True,
                              text=True)
    except FileNotFoundError as exc:
        raise Refusal("a run on a git ref needs git, and there is no `git` "
                      "on the PATH") from exc
    if done.returncode != 0:
        raise Refusal("git %s failed: %s" % (args[0], done.stderr.strip()))
    return done.stdout


def _mark(where: str, root: str) -> None:
    """Write *where*'s marker: the pid of this process and the project it is
    a copy of."""
    with open(os.path.join(where, MARKER), "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "root": root}, fh)


# Windows: what `OpenProcess` is asked for, what it says of a pid nothing has,
# and what `GetExitCodeProcess` says of a process still running. Each on a
# line of its own because the Linux sweep cannot reach them.
_QUERY_LIMITED = 0x1000  # invective: accept[untestable: 4096 -> 4097] Windows only, and the sweep runs on Linux
_INVALID_PARAMETER = 87  # invective: accept[untestable: 87 -> 88] Windows only, and the sweep runs on Linux
_STILL_ACTIVE = 259  # invective: accept[untestable: 259 -> 260] Windows only, and the sweep runs on Linux


def _alive(pid: int) -> bool:
    """Whether a process with *pid* is running. One that cannot be asked is
    somebody else's, and counts as running: a copy is kept until its owner
    is known to be gone, never removed on a doubt."""
    if os.name == "nt":
        # **Not `os.kill(pid, 0)` here.** On Windows `os.kill` with any
        # signal but the two console events calls `TerminateProcess`, so
        # the probe would kill the owner it asks after. A handle on the
        # process instead: none for a pid nothing has, and a process that
        # has ended but is still held open somewhere (a `Popen` not yet
        # collected) has an exit code that is not `STILL_ACTIVE`.
        import ctypes
        # invective: accept[untestable: True -> False] Windows only, and the sweep runs on Linux
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        # invective: accept[untestable: False -> True] Windows only, and the sweep runs on Linux
        handle = kernel.OpenProcess(_QUERY_LIMITED, False, pid)
        # invective: accept[untestable: not X -> X] Windows only, and the sweep runs on Linux
        if not handle:
            # invective: accept[untestable: NotEq -> Eq] Windows only, and the sweep runs on Linux
            return ctypes.get_last_error() != _INVALID_PARAMETER
        code = ctypes.c_ulong()
        try:
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        finally:
            kernel.CloseHandle(handle)
        # invective: accept[untestable: Eq -> NotEq] Windows only, and the sweep runs on Linux
        return code.value == _STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Running, and not ours to signal.
        pass
    return True


def reap() -> None:
    """Remove every copy in the temporary directory whose owner is dead.

    Run at the start of every copy, deliberately: that is every `run`, every
    engine the sweep starts, every `pytest --mutate`, and every test of
    invective's own that makes one. It is one listing of the temporary
    directory and a marker read per `invective-*` entry, and a reaper that
    ran only from a command's `main` would leave the `pytest --mutate` path,
    which has none, with no reaper at all.

    Only a marked directory is a copy: the engine's `invective-verdict-*`,
    `invective-selection-*` and `invective-sweep-*` boxes have no marker,
    and nor has a copy whose owner was killed between making the directory
    and marking it. A dead owner's copy is renamed first, atomically, to
    `invective-dead-<name>` and removed under that name: a reaper killed
    halfway through may have deleted the marker before the rest, and the
    next reaper still knows the directory for what it is. The suffix
    `mkdtemp` gives a copy never starts `dead-`, so the two cannot collide.
    A rename that fails is another reaper's win, or a copy on a shared
    temporary directory that is not ours to touch, and is left.

    **Pid liveness, not a lock.** A lock the OS drops on death would be
    immune to pid reuse, but on Windows a held handle inside the copy blocks
    the copy's own removal. A pid in a file is readable by a person and
    fails in the safe direction: a dead owner's pid taken by an unrelated
    process keeps the copy until that pid is free, a delayed reap and never
    a wrong one, and a zombie owner reads as alive until its parent collects
    it. A `--ref` copy holds a git worktree, whose entry in the repository's
    `.git/worktrees` `git_ref` clears on its own way out; the entry of one
    removed here is dropped by the `prune` at the end of the next `--ref`
    run, unless a kill during its checkout left it locked. No git is run
    here, so a plain run still asks git nothing, whatever sits in the
    temporary directory.
    """
    box = tempfile.gettempdir()
    try:
        names = os.listdir(box)
    except OSError:
        return
    for name in names:
        if not name.startswith("invective-"):
            continue
        path = os.path.join(box, name)
        if name.startswith("invective-dead-"):
            shutil.rmtree(path, ignore_errors=True)
            continue
        try:
            with open(os.path.join(path, MARKER), encoding="utf-8") as fh:
                pid = json.load(fh)["pid"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        # A marker that does not hold a pid is not ours, and is left like one
        # that cannot be parsed: a stray file must not stop every start. No
        # OS invective runs on hands out a pid of 2 ** 32 or more (a signed
        # `pid_t` on POSIX, a DWORD on Windows), and on Windows ctypes refuses
        # one with an `ArgumentError`, which is no `OverflowError`.
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        if pid <= 0 or pid >= 2 ** 32:
            continue
        try:
            owner_alive = pid == os.getpid() or _alive(pid)
        # An `OverflowError` from `os.kill` past 2 ** 31 on POSIX.
        except (OverflowError, ValueError, OSError):
            continue
        if owner_alive:
            continue
        dead = os.path.join(box, "invective-dead-" + name[len("invective-"):])
        try:
            os.rename(path, dead)
        except OSError:
            continue
        shutil.rmtree(dead, ignore_errors=True)
