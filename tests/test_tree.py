"""The copy the mutants are written in: what it holds, and what it leaves out."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

import pytest

from invective import mutate
from invective import tree as trees

from conftest import git, slow_run, stop_group, write_tree


def _files(where):
    # The copy's own marker is not a file of the project's.
    return sorted(os.path.relpath(os.path.join(d, f), where).replace(os.sep, "/")
                  for d, _dirs, files in os.walk(where) for f in files
                  if f != trees.MARKER)


def test_the_copy_leaves_out_version_control_caches_and_environments(tmp_path):
    """Untracked files are the project's too; a virtual environment is left
    out by the `pyvenv.cfg` every one has, whatever it is called."""
    root = str(tmp_path / "project")
    write_tree(root, {
        "pkg/a.py": "x = 1\n",
        "notes/untracked.txt": "kept\n",
        ".git/HEAD": "ref\n",
        "pkg/__pycache__/a.cpython.pyc": "",
        ".pytest_cache/v": "",
        "env-of-any-name/pyvenv.cfg": "home = /usr\n",
        "env-of-any-name/lib/site.py": "",
        "var/data.bin": "",
        "var/keep/readme.txt": "",
    })

    with trees.working_tree(root, ("var/*.bin",)) as where:
        assert _files(where) == ["notes/untracked.txt", "pkg/a.py",
                                 "var/keep/readme.txt"]
    assert not os.path.exists(where)


def test_a_temporary_directory_inside_the_project_is_not_copied_into_itself(
        tmp_path, monkeypatch):
    root = str(tmp_path / "project")
    write_tree(root, {"pkg/a.py": "x = 1\n"})
    inside = os.path.join(root, "tmp-of-the-project")
    monkeypatch.setattr(trees.tempfile, "mkdtemp",
                        lambda prefix: os.makedirs(inside) or inside)

    with trees.working_tree(root) as where:
        assert where == inside
        assert _files(where) == ["pkg/a.py"]


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="a symbolic link without privileges")
def test_a_module_that_is_a_link_out_of_the_project_is_refused(tmp_path,
                                                               monkeypatch):
    """Written through, the mutant would land in the file the link names."""
    real = tmp_path / "elsewhere.py"
    real.write_text("def f(n):\n    if n < 0:\n        raise ValueError\n",
                    encoding="utf-8")
    root = tmp_path / "project"
    write_tree(str(root), {"tests/test_f.py": "def test_f():\n    pass\n"})
    os.symlink(real, root / "f.py")
    monkeypatch.setattr(mutate, "run_tests",
                        lambda *a, **k: mutate.Verdict(True, 0, "", ""))

    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(str(root), str(root / "f.py"), ["tests/test_f.py"],
                      None, None)
    assert "through a link" in str(caught.value)
    assert "n < 0" in real.read_text(encoding="utf-8")


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="a symbolic link without privileges")
def test_a_link_inside_the_project_is_written_through(tmp_path):
    """The tests import the file the link names, so that is where the mutant
    has to be for them to see it: here the refusal is checked, and its
    removal killed."""
    root = tmp_path / "project"
    write_tree(str(root), {
        "pkg/__init__.py": "",
        "pkg/real.py": ("def f(n):\n    if n < 0:\n"
                        "        raise ValueError\n    return n\n"),
        "tests/test_f.py": ("import pytest\nfrom pkg import real\n\n"
                            "def test_f():\n    with pytest.raises(ValueError):\n"
                            "        real.f(-1)\n"),
    })
    os.symlink("real.py", root / "pkg" / "alias.py")

    report = mutate.mutate(str(root), str(root / "pkg" / "alias.py"),
                           ["tests/test_f.py"], ["RAISE"], None)

    assert report["killed"] == report["mutants"] == 1


def test_a_ref_needs_git_and_says_so_when_there_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(mutate.Refusal) as caught:
        with trees.git_ref(str(tmp_path), "HEAD"):
            pass
    assert "needs git" in str(caught.value)
    assert not [n for n in os.listdir(tmp_path) if n.startswith("invective-")]


def test_a_ref_without_git_is_refused_even_when_its_directory_will_not_go(
        tmp_path, monkeypatch):
    """The refusal is what the person is told; the cleanup's own failure
    does not replace it."""
    monkeypatch.setenv("PATH", str(tmp_path))
    real = os.rmdir

    def rmdir(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)).startswith("invective-"):
            raise PermissionError(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", rmdir)

    with pytest.raises(mutate.Refusal):
        with trees.git_ref(str(tmp_path), "HEAD"):
            pass


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="a symbolic link without privileges")
def test_a_link_to_a_directory_is_copied_as_a_link_and_not_followed(tmp_path):
    """Followed, a link back up the tree would be copied without end."""
    root = tmp_path / "project"
    write_tree(str(root), {"pkg/a.py": "x = 1\n"})
    os.symlink(root, root / "pkg" / "loop", target_is_directory=True)

    with trees.working_tree(str(root)) as where:
        assert os.path.islink(os.path.join(where, "pkg", "loop"))


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="a file its owner cannot read")
def test_a_project_that_cannot_be_copied_is_refused(tmp_path):
    root = tmp_path / "project"
    write_tree(str(root), {"pkg/a.py": "x = 1\n", "pkg/secret.py": "y = 2\n"})
    os.chmod(root / "pkg" / "secret.py", 0)
    try:
        with pytest.raises(mutate.Refusal) as caught:
            with trees.working_tree(str(root)):
                pass
    finally:
        os.chmod(root / "pkg" / "secret.py", 0o644)
    assert str(caught.value).startswith("could not copy ")


def test_a_copy_that_will_not_be_removed_does_not_replace_the_outcome(
        tmp_path, monkeypatch):
    """What happened in the copy is the run's to report, not the cleanup's."""
    root = str(tmp_path / "project")
    write_tree(root, {"pkg/a.py": "x = 1\n"})
    real = trees.shutil.rmtree

    def rmtree(path, ignore_errors=False):
        if not ignore_errors:
            raise PermissionError(path)
        real(path, ignore_errors=True)

    monkeypatch.setattr(trees.shutil, "rmtree", rmtree)
    with pytest.raises(KeyError):
        with trees.working_tree(root):
            raise KeyError("the run's own outcome")


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="a symbolic link without privileges")
def test_a_module_reached_through_a_linked_directory_is_refused(
        tmp_path, monkeypatch):
    """The copy keeps a directory link as a link, so the module's path in the
    copy leads out of it, and a mutant written there would land in the file
    the link reaches: in the project, or anywhere else."""
    elsewhere = tmp_path / "elsewhere"
    write_tree(str(elsewhere), {"gate.py": "def f(n):\n    if n < 0:\n"
                                           "        raise ValueError\n"})
    root = tmp_path / "project"
    write_tree(str(root), {"tests/test_f.py": "def test_f():\n    pass\n"})
    os.symlink(elsewhere, root / "pkg", target_is_directory=True)
    monkeypatch.setattr(mutate, "run_tests",
                        lambda *a, **k: mutate.Verdict(True, 0, "", ""))
    before = (elsewhere / "gate.py").read_text(encoding="utf-8")

    with pytest.raises(mutate.Refusal) as caught:
        mutate.mutate(str(root), str(root / "pkg" / "gate.py"),
                      ["tests/test_f.py"], None, None)
    assert "through a link" in str(caught.value)
    assert (elsewhere / "gate.py").read_text(encoding="utf-8") == before


def _marked(box):
    return sorted(n for n in os.listdir(box) if n.startswith("invective-"))


def test_a_copy_whose_owner_was_killed_is_removed_at_the_next_start(tree,
                                                                     tmp_path):
    """A SIGKILL runs no `finally`: the copy stays, a whole working tree,
    marked with the pid of a process that is gone, and the next copy to be
    made begins by removing it."""
    with slow_run(tree, tmp_path) as (proc, where, run):
        proc.kill()
        # The run goes on in a group of its own, and holds the copy as its
        # working directory until it is stopped.
        stop_group(run)
        # Collected, not merely dead: a killed child is a zombie until its
        # parent waits on it, and a zombie still answers `os.kill(pid, 0)`,
        # so the reap would read the owner as alive and rightly keep its
        # copy.
        proc.wait()
        assert os.path.exists(where)

        with trees.working_tree(tree):
            assert not os.path.exists(where)


def test_a_live_owner_s_copy_is_never_removed(tmp_path):
    """A dead owner's copy goes, and nothing else: not a live owner's, and
    not a directory with no marker, which is one of the engine's own boxes or
    a copy whose owner has not marked it yet. The two halves are one test
    because the first passes vacuously where there is no reaper at all."""
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait()
    try:
        for name, pid in (("invective-aaaaaaaa", live.pid),
                          ("invective-bbbbbbbb", gone.pid)):
            os.mkdir(tmp_path / name)
            (tmp_path / name / trees.MARKER).write_text(
                json.dumps({"pid": pid, "root": "elsewhere"}), encoding="utf-8")
        os.mkdir(tmp_path / "invective-verdict-x")

        trees.reap()

        assert _marked(tmp_path) == ["invective-aaaaaaaa", "invective-verdict-x"]
        # And still running half a second on: the probe has to be the signal
        # that does nothing, not one that stops the owner it asks after.
        with pytest.raises(subprocess.TimeoutExpired):
            live.wait(timeout=0.5)
        live.kill()
        live.wait()

        trees.reap()

        assert _marked(tmp_path) == ["invective-verdict-x"]
    finally:
        live.kill()
        live.wait()


def test_the_copy_is_marked_with_its_owner_before_it_is_filled(tmp_path,
                                                                monkeypatch):
    """A run killed halfway through the copying still leaves a marked
    directory, so the marker goes in first."""
    root = str(tmp_path / "project")
    write_tree(root, {"pkg/a.py": "x = 1\n"})
    seen = []

    def copytree(src, dst, **kwargs):
        with open(os.path.join(dst, trees.MARKER), encoding="utf-8") as fh:
            seen.append(json.load(fh))
        raise OSError("killed halfway")

    monkeypatch.setattr(trees.shutil, "copytree", copytree)
    with pytest.raises(mutate.Refusal):
        with trees.working_tree(root):
            pass
    assert seen == [{"pid": os.getpid(), "root": root}]


def test_a_ref_s_copy_is_marked_with_its_owner_too(repo, tmp_path):
    """A worktree is a copy like any other, marked at the top of the
    directory it is checked out into."""
    with trees.git_ref(repo, "HEAD"):
        (box,) = _marked(tmp_path)
        with open(tmp_path / box / trees.MARKER, encoding="utf-8") as fh:
            assert json.load(fh) == {"pid": os.getpid(), "root": repo}


def test_a_ref_s_copy_is_marked_before_git_fills_it(repo, monkeypatch):
    """The checkout of a large tree takes seconds, and a kill of the whole
    process group during it, git included, must leave a directory the next
    reaper knows for a copy."""
    real = trees._git
    seen = []

    def checking_the_marker(root, *args):
        if args[:2] == ("worktree", "add"):
            seen.append(os.path.isfile(os.path.join(
                os.path.dirname(args[3]), trees.MARKER)))
        return real(root, *args)

    monkeypatch.setattr(trees, "_git", checking_the_marker)
    with trees.git_ref(repo, "HEAD"):
        pass
    assert seen == [True]


def test_a_copy_killed_while_it_is_removed_is_left_under_a_name_the_reaper_removes(
        tmp_path, monkeypatch):
    """Removing a large tree takes seconds and may delete the marker first,
    and a kill then would leave an unmarked directory no reaper touches. The
    copy is renamed before the removal, as the reaper renames its own."""
    root = str(tmp_path / "project")
    write_tree(root, {"pkg/a.py": "x = 1\n"})
    real = trees.shutil.rmtree
    removed = []
    # The removal is never done: what is left is the most a kill during it
    # could leave.
    monkeypatch.setattr(trees.shutil, "rmtree",
                        lambda path, ignore_errors=False: removed.append(path))

    with trees.working_tree(root):
        pass

    left = removed[-1]
    assert os.path.basename(left).startswith("invective-dead-")
    assert os.path.isfile(os.path.join(left, "pkg", "a.py"))
    monkeypatch.setattr(trees.shutil, "rmtree", real)
    trees.reap()
    assert _marked(tmp_path) == []


def test_a_marker_at_the_project_s_top_is_not_copied_over_the_copy_s(tmp_path):
    """A project holding one -- the leaked copy of something, being measured
    -- would otherwise hand the copy a stale pid."""
    root = str(tmp_path / "project")
    write_tree(root, {"pkg/a.py": "x = 1\n",
                      trees.MARKER: json.dumps({"pid": 1, "root": "elsewhere"})})

    with trees.working_tree(root) as where:
        with open(os.path.join(where, trees.MARKER), encoding="utf-8") as fh:
            assert json.load(fh)["pid"] == os.getpid()


def test_a_ref_stopped_during_the_checkout_leaves_no_copy_and_no_worktree(
        repo, tmp_path, monkeypatch):
    """A SIGTERM or ^C that lands while git is filling the worktree leaves
    its entry locked, which `prune` never drops: the directory and the entry
    are both removed on the way out."""
    real = trees._git

    def stopped_after_the_add(root, *args):
        out = real(root, *args)
        if args[:2] == ("worktree", "add"):
            # As git leaves it when the stop lands before it has finished.
            git(repo, "worktree", "lock", args[3])
            raise mutate._Terminated(signal.SIGTERM)
        return out

    monkeypatch.setattr(trees, "_git", stopped_after_the_add)
    with pytest.raises(mutate._Terminated):
        with trees.git_ref(repo, "HEAD"):
            pass

    assert not [n for n in os.listdir(tmp_path) if n.startswith("invective-")]
    listed = [line for line in git(repo, "worktree", "list",
                                   "--porcelain").splitlines()
              if line.startswith("worktree ")]
    # The repository's own, alone.
    assert len(listed) == 1


@pytest.mark.parametrize("text", [
    json.dumps({"pid": "123"}),
    json.dumps({"pid": None}),
    json.dumps({"pid": 1.5}),
    json.dumps({"pid": 10 ** 20}),
    # Past what a pid is on any OS: on Windows ctypes refuses it with an
    # error that is not an `OverflowError`.
    json.dumps({"pid": 2 ** 32}),
    json.dumps({"pid": True}),
    # Negative, a process group to `os.kill`, and one nothing has.
    json.dumps({"pid": -(2 ** 31 - 1)}),
    # The caller's own process group to `os.kill`.
    json.dumps({"pid": 0}),
    "[]",
    "not json",
], ids=["string", "null", "float", "too-large", "past-32-bits", "bool",
        "negative", "zero", "list", "unparsable"])
def test_a_marker_that_holds_no_pid_is_left_and_stops_nothing(tmp_path, text,
                                                              monkeypatch):
    """A stray file in the temporary directory is not an owner to ask about,
    and must not make every start of invective fail."""
    # Not asked at all: what the OS would say of such a number differs by
    # platform, and on Linux a probe of `True` is a probe of pid 1.
    monkeypatch.setattr(trees, "_alive",
                        lambda pid: pytest.fail("asked about %r" % (pid,)))
    stray = tmp_path / "invective-cccccccc"
    os.mkdir(stray)
    (stray / trees.MARKER).write_text(text, encoding="utf-8")

    trees.reap()

    assert stray.is_dir()


def test_every_pid_an_os_hands_out_is_asked_about(tmp_path, monkeypatch):
    """The bounds a marker's pid is held to leave out none that a process
    can have: from 1 to the last 32-bit number, a Windows pid."""
    asked = []
    monkeypatch.setattr(trees, "_alive",
                        lambda pid: asked.append(pid) or False)
    for name, pid in (("invective-11111111", 1),
                      ("invective-22222222", 2 ** 32 - 1)):
        os.mkdir(tmp_path / name)
        (tmp_path / name / trees.MARKER).write_text(
            json.dumps({"pid": pid, "root": "elsewhere"}), encoding="utf-8")

    trees.reap()

    assert sorted(asked) == [1, 2 ** 32 - 1]
    assert _marked(tmp_path) == []


def _dead_owners_copy(tmp_path, name):
    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait()
    dead = tmp_path / name
    os.mkdir(dead)
    (dead / trees.MARKER).write_text(
        json.dumps({"pid": gone.pid, "root": "elsewhere"}), encoding="utf-8")
    return dead


def test_a_ref_begins_by_removing_the_copies_of_dead_owners(repo, tmp_path):
    """`working_tree` is covered by the test of the killed run; the ref's own
    start has to reap as well."""
    dead = _dead_owners_copy(tmp_path, "invective-dddddddd")

    with trees.git_ref(repo, "HEAD"):
        assert not dead.exists()


def _refuse_to_remove_directories(monkeypatch):
    """Every directory removal under an `invective-` name fails, as one does
    on Windows for a file the stopped run held open."""
    real = os.rmdir

    def rmdir(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)).startswith("invective-"):
            raise PermissionError(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", rmdir)


def test_a_dead_owner_s_copy_that_will_not_be_removed_does_not_stop_the_start(
        tmp_path, monkeypatch):
    _dead_owners_copy(tmp_path, "invective-eeeeeeee")
    _refuse_to_remove_directories(monkeypatch)

    trees.reap()


def test_a_copy_already_renamed_for_removal_that_will_not_go_does_not_stop_the_start(
        tmp_path, monkeypatch):
    """What a reaper killed halfway leaves, met by the next one."""
    os.mkdir(tmp_path / "invective-dead-ffffffff")
    _refuse_to_remove_directories(monkeypatch)

    trees.reap()
