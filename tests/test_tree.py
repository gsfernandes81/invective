"""The copy the mutants are written in: what it holds, and what it leaves out."""

from __future__ import annotations

import os

import pytest

from invective import mutate
from invective import tree as trees

from conftest import write_tree


def _files(where):
    return sorted(os.path.relpath(os.path.join(d, f), where).replace(os.sep, "/")
                  for d, _dirs, files in os.walk(where) for f in files)


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
