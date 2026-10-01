"""A small git repository to mutate, built fresh for each test that asks."""

from __future__ import annotations

import os
import subprocess

import pytest

#: The fixture repository, as path -> text. Its tests live INSIDE the package,
#: which is the layout where a sweep has to tell the suite's own files from
#: the modules it is there to mutate.
FILES = {
    "pkg/__init__.py": "",
    # Line 2 is refused by a test; the `and` on line 4 is checked by nothing.
    "pkg/gate.py": (
        "def admit(age, member):\n"
        "    if age < 18:\n"
        "        raise ValueError('under age')\n"
        "    if member and age >= 65:\n"
        "        return 'senior'\n"
        "    return 'adult'\n"),
    "pkg/idle.py": (
        "def never():\n"
        "    raise RuntimeError('nothing imports this module')\n"),
    "pkg/sub/__init__.py": "",
    "pkg/sub/deep.py": (
        "def deep(x):\n"
        "    if x is None:\n"
        "        raise ValueError('nothing given')\n"
        "    return x\n"),
    "pkg/tests_extra/aid.py": "def aid():\n    return 1\n",
    # Neither of these two is a module to mutate.
    "pkg/notes.txt": "not python\n",
    "pkg/sub/test_inline.py": "def test_inline():\n    pass\n",
    "pkg/tests/conftest.py": (
        "import os\n"
        "import sys\n"
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..',"
        " 'loose'))\n"),
    # Imports `gate` and `tool`, and is still not a test file.
    "pkg/tests/helpers.py": (
        "import tool\n"
        "from pkg import gate\n"
        "\n"
        "def helper():\n"
        "    raise RuntimeError('harness')\n"),
    "pkg/tests/test_gate.py": (
        "import pytest\n"
        "from pkg import gate\n"
        "\n"
        "def test_a_minor_is_refused():\n"
        "    with pytest.raises(ValueError):\n"
        "        gate.admit(10, False)\n"
        "\n"
        "def test_an_adult_is_admitted():\n"
        "    assert gate.admit(30, False) == 'adult'\n"),
    "pkg/tests/test_deep_things.py": (
        "import pytest\n"
        "from pkg.sub import deep\n"
        "\n"
        "def test_nothing_is_refused():\n"
        "    with pytest.raises(ValueError):\n"
        "        deep.deep(None)\n"),
    # The refusal in `tool` is checked by nothing here.
    "pkg/tests/test_tool.py": (
        "import tool\n"
        "\n"
        "def test_a_count_comes_back():\n"
        "    assert tool.tool(3) == 3\n"),
    "loose/tool.py": (
        "def tool(n):\n"
        "    if n < 0:\n"
        "        raise ValueError('negative')\n"
        "    return n\n"),
}


def write_tree(root, files):
    for rel, text in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


def git(root, *args):
    done = subprocess.run(["git", "-C", root, *args], capture_output=True,
                          text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def commit(root, files=None):
    """Write *files* into the repository at *root*, and commit everything."""
    write_tree(root, files or {})
    git(root, "add", "-A")
    git(root, "-c", "user.name=fixture", "-c",
        "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "-q", "-m", "fixture")


@pytest.fixture
def tree(tmp_path):
    """The fixture files on disk, with no repository around them."""
    root = os.path.realpath(tmp_path / "tree")
    write_tree(root, FILES)
    return root


@pytest.fixture
def repo(tree, monkeypatch):
    """The fixture files committed to a repository, and the cwd inside it."""
    git(tree, "init", "-q", "-b", "main")
    commit(tree)
    monkeypatch.chdir(tree)
    return tree
