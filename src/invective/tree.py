"""Where a campaign's mutants are written: a copy of the project as it stands,
or of one of its commits.

Either is a directory of its own, made before the first run and removed after
the last, so the files a person is working on never hold a mutant, and a run
that is killed leaves nothing behind in them. Each is a context manager that
gives the path of the copy's top level.
"""

from __future__ import annotations

import contextlib
import fnmatch
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


@contextlib.contextmanager
def working_tree(root: str, exclude: tuple[str, ...] = ()):
    """A copy of *root* as it stands, uncommitted edits and untracked files
    included. *exclude* holds glob patterns, matched against paths relative to
    *root* with `/` between their parts, of anything else to leave out."""
    where = tempfile.mkdtemp(prefix="invective-")
    root = os.path.abspath(root)

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
            elif any(fnmatch.fnmatchcase(rel, pattern) for pattern in exclude):
                left.add(name)
        return left

    try:
        try:
            shutil.copytree(root, where, symlinks=True, ignore=ignore,
                            dirs_exist_ok=True)
        except (shutil.Error, OSError) as exc:
            raise Refusal("could not copy %s: %s" % (root, exc)) from exc
        yield where
    finally:
        shutil.rmtree(where, ignore_errors=True)


@contextlib.contextmanager
def git_ref(root: str, ref: str):
    """The tree of commit *ref* of the git repository *root* is in, as a
    detached git worktree; the copy's top level is the directory there that
    corresponds to *root*. The only part of invective that needs git."""
    where = tempfile.mkdtemp(prefix="invective-")
    try:
        prefix = _git(root, "rev-parse", "--show-prefix").strip()
        _git(root, "worktree", "add", "--detach", where, ref)
    except Refusal:
        shutil.rmtree(where, ignore_errors=True)
        raise
    try:
        yield os.path.join(where, *prefix.split("/")) if prefix else where
    finally:
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force",
                        where], capture_output=True, text=True)
        shutil.rmtree(where, ignore_errors=True)
        # When `remove` failed -- on Windows a file the stopped run held open
        # is enough -- the directory is gone now but git still lists it.
        subprocess.run(["git", "-C", root, "worktree", "prune"],
                       capture_output=True, text=True)


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
