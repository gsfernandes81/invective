"""What invective keeps in a project between runs: the `.invective/`
directory, its files written whole or not at all, the killer history, and
the verdict cache: its key, what it keeps and what a run reads of it."""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import sys
from types import SimpleNamespace

import pytest

from invective import mutate, store, tree

from conftest import write_tree

TARGET = "pkg/gate.py"


def _said():
    lines = []
    return lines, lines.append


def _read(root):
    with open(os.path.join(root, store.DIRECTORY, store.HISTORY),
              encoding="utf-8") as fh:
        return json.load(fh)


def _write(root, data):
    os.makedirs(os.path.join(root, store.DIRECTORY), exist_ok=True)
    with open(os.path.join(root, store.DIRECTORY, store.HISTORY), "w",
              encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))


# --------------------------------------------------------------------------
# The directory


def test_the_directory_ignores_itself(tmp_path):
    """Nothing in it is ever committed, and no project has to say so: the
    text is exact, with `\\n` line endings on every platform."""
    where = store.directory(str(tmp_path))
    assert where == os.path.join(str(tmp_path), ".invective")
    with open(os.path.join(where, ".gitignore"), "rb") as fh:
        assert fh.read() == b"# Created by invective automatically.\n*\n"


def test_a_racing_creator_is_not_an_error(tmp_path):
    """Two runs making it at once: the second finds the directory, and the
    `.gitignore`, there already, and leaves the file as it found it."""
    store.directory(str(tmp_path))
    ignore = tmp_path / ".invective" / ".gitignore"
    ignore.write_text("mine\n", encoding="utf-8")
    store.directory(str(tmp_path))
    assert ignore.read_text(encoding="utf-8") == "mine\n"


# --------------------------------------------------------------------------
# A file written whole or not at all


def test_a_file_is_written_whole_and_its_temporary_is_gone(tmp_path):
    lines, say = _said()
    path = str(tmp_path / "f.json")
    store.write_json(path, {"a": 1}, say)
    assert store.read_json(path) == {"a": 1}
    assert os.listdir(tmp_path) == ["f.json"]
    assert lines == []


@pytest.mark.skipif(os.name == "nt", reason="Windows has no mode bits")
@pytest.mark.parametrize("umask, mode", [(0o022, 0o644), (0o027, 0o640)])
def test_a_file_written_has_the_mode_the_umask_leaves(tmp_path, umask, mode):
    """Not owner-only: another user of a shared checkout reads the history,
    and a file it cannot read is a history it runs without."""
    path = str(tmp_path / "f.json")
    old = os.umask(umask)
    try:
        store.write_json(path, {}, print)
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(path).st_mode) == mode


def test_a_failed_replace_leaves_the_old_file_and_no_temporary(tmp_path,
                                                              monkeypatch):
    path = str(tmp_path / "f.json")
    lines, say = _said()
    store.write_json(path, {"old": True}, say)

    def fail(src, dst):
        raise OSError("the disk is full")

    monkeypatch.setattr(store.os, "replace", fail)
    store.write_json(path, {"new": True}, say)
    assert store.read_json(path) == {"old": True}
    assert os.listdir(tmp_path) == ["f.json"]
    assert lines == ["warning:   %s could not be written, and is left as it "
                     "was: the disk is full" % path]


@pytest.mark.parametrize("held, written", [(3, True), (4, False)])
def test_a_held_file_is_retried_then_warned(tmp_path, monkeypatch, held,
                                            written):
    """A reader holding the file on Windows makes `os.replace` fail with
    `PermissionError` for a moment: the write is tried three times more,
    then given up, and said."""
    path = str(tmp_path / "f.json")
    real = os.replace
    tries = []

    def replace(src, dst):
        tries.append(src)
        if len(tries) <= held:
            raise PermissionError("held")
        real(src, dst)

    monkeypatch.setattr(store, "RETRY_WAIT", 0)
    monkeypatch.setattr(store.os, "replace", replace)
    lines, say = _said()
    store.write_json(path, [1], say)
    assert len(tries) == 4
    assert (store.read_json(path) == [1]) is written
    assert bool(lines) is not written
    assert os.listdir(tmp_path) == (["f.json"] if written else [])


def test_old_temporaries_are_swept(tmp_path, monkeypatch):
    """What a writer killed outright leaves, once it is older than any
    write takes, and nothing else."""
    now = 2_000_000_000
    monkeypatch.setattr(store.time, "time", lambda: now)
    ages = {"tmpold.json": store.STALE + 1, "tmpdue.json": store.STALE,
            "tmpnew.json": store.STALE - 1, "old.json": 10 * store.STALE}
    for name, age in ages.items():
        (tmp_path / name).write_text("{}", encoding="utf-8")
        os.utime(tmp_path / name, (now - age, now - age))
    store.sweep_temporaries(str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == ["old.json", "tmpdue.json",
                                            "tmpnew.json"]
    assert store.STALE == 24 * 60 * 60


def test_a_file_that_is_not_json_is_unreadable_and_none_is_no_file(tmp_path):
    assert store.read_json(str(tmp_path / "none.json")) is None
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    with pytest.raises(store.Unreadable):
        store.read_json(str(tmp_path / "bad.json"))


# --------------------------------------------------------------------------
# The keys


def _keys(source):
    module = mutate._Module(source)
    return store.mutant_keys(mutate._report_lines(source), module.sites)


def test_a_key_is_what_the_mutant_is():
    assert [json.loads(key) for key in _keys("def f(x):\n    return x < 1\n")
            ] == [["CMP", "Lt -> LtE", "x < 1", "return x < 1", 0],
                  ["CONST", "1 -> 2", "1", "return x < 1", 0]]


def test_a_key_survives_edits_elsewhere_in_the_file():
    source = "def f(x):\n    if x < 1:\n        raise ValueError\n"
    edited = "import os\n\n\n" + source.replace("def f", "def g") + "y = 2\n"
    assert set(_keys(source)) < set(_keys(edited))


def test_identical_sites_get_their_own_ordinal():
    keys = _keys("x = 1\nx = 1\ny = 1\n")
    assert [json.loads(key)[-1] for key in keys] == [0, 1, 0]
    assert len(set(keys)) == 3


def test_a_node_ast_unparse_cannot_write_is_keyed_by_its_dump(monkeypatch):
    """Before 3.12 `ast.unparse` raises on some f-strings."""
    def unparse(node):
        raise ValueError("cannot")

    monkeypatch.setattr(store.ast, "unparse", unparse)
    (key,) = _keys("x = 1\n")
    assert json.loads(key)[2] == ast.dump(ast.Constant(1))


@pytest.mark.parametrize("killer, kept", [
    ("pkg/tests/test_gate.py::test_a", True), ("pkg/tests/test_gate.py", False),
    ("TIMEOUT", False), ("", False)])
def test_only_a_test_is_a_killer_to_keep(killer, kept):
    assert store.storable(killer) is kept


# --------------------------------------------------------------------------
# The history


def test_a_history_is_read_for_its_target_only(tmp_path):
    _write(str(tmp_path), {TARGET: {"k": "t::a"}, "other.py": {"k": "t::b"}})
    lines, say = _said()
    history = store.History.load(str(tmp_path), TARGET, say)
    assert history.killers == {"k": "t::a"}
    assert store.History.load(str(tmp_path), "none.py", say).killers == {}
    assert lines == []


@pytest.mark.parametrize("text", [
    "{", "[]", '{"pkg/gate.py": []}', '{"pkg/gate.py": {"k": 1}}'])
def test_a_corrupt_history_is_ignored_with_a_warning(tmp_path, text):
    _write(str(tmp_path), text)
    lines, say = _said()
    history = store.History.load(str(tmp_path), TARGET, say)
    assert history.killers == {}
    (line,) = lines
    assert line.startswith("warning:   %s cannot be read, and is ignored: "
                           % os.path.join(str(tmp_path), ".invective",
                                          "history.json"))
    # Written over by the next save, with this run's changes alone.
    history.note("k", False, "t::a")
    history.save(["k"])
    assert _read(str(tmp_path)) == {TARGET: {"k": "t::a"}}


def test_a_kill_remembers_its_killer_and_anything_else_forgets(tmp_path):
    _write(str(tmp_path), {TARGET: {"a": "t::old", "b": "t::old",
                                    "c": "t::old", "d": "t::old",
                                    "e": "t::old"}})
    history = store.History.load(str(tmp_path), TARGET, print)
    history.note("a", False, "t::new")
    history.note("b", True, "")
    history.note("c", False, "TIMEOUT")
    history.note("d", False, "pkg/tests/test_gate.py")
    history.save("abcde")
    assert _read(str(tmp_path)) == {TARGET: {"a": "t::new", "e": "t::old"}}


def test_a_save_keeps_another_target_written_meanwhile(tmp_path):
    """Two campaigns at once, on two modules: each writes its own changes
    over the file as it finds it."""
    history = store.History.load(str(tmp_path), TARGET, print)
    _write(str(tmp_path), {"other.py": {"k": "t::b"}})
    history.note("k", False, "t::a")
    history.save(["k"])
    assert _read(str(tmp_path)) == {"other.py": {"k": "t::b"},
                                    TARGET: {"k": "t::a"}}


def test_a_site_gone_is_pruned_and_a_target_with_none_left_is_dropped(
        tmp_path):
    _write(str(tmp_path), {TARGET: {"gone": "t::a", "here": "t::b"},
                           "other.py": {"gone": "t::c"}})
    history = store.History.load(str(tmp_path), TARGET, print)
    history.save(["here"])
    assert _read(str(tmp_path)) == {TARGET: {"here": "t::b"},
                                    "other.py": {"gone": "t::c"}}
    history.save([])
    assert _read(str(tmp_path)) == {"other.py": {"gone": "t::c"}}


def test_nothing_changed_writes_nothing(tmp_path):
    history = store.History.load(str(tmp_path), TARGET, print)
    history.note("k", True, "")
    history.save(["k"])
    assert not os.path.exists(tmp_path / ".invective")
    _write(str(tmp_path), {TARGET: {"k": "t::a"}})
    os.utime(tmp_path / ".invective" / "history.json", (1, 1))
    history = store.History.load(str(tmp_path), TARGET, print)
    history.note("k", False, "t::a")
    history.save(["k"])
    assert os.path.getmtime(tmp_path / ".invective" / "history.json") == 1


def test_the_history_is_written_in_a_directory_that_ignores_itself(tmp_path):
    history = store.History.load(str(tmp_path), TARGET, print)
    history.note("k", False, "t::a")
    history.save(["k"])
    assert sorted(os.listdir(tmp_path / ".invective")) == [".gitignore",
                                                           "history.json"]


def test_a_directory_that_cannot_be_made_is_said_and_nothing_raised(
        tmp_path):
    (tmp_path / ".invective").write_text("a file", encoding="utf-8")
    lines, say = _said()
    history = store.History.load(str(tmp_path), TARGET, say)
    history.note("k", False, "t::a")
    history.save(["k"])
    assert lines[-1].startswith("warning:   %s could not be made, so the "
                                "history is not kept: "
                                % (tmp_path / ".invective"))


# --------------------------------------------------------------------------
# The verdict cache: its key

#: A project whose tests are in `tests/`, beside a source tree in `src/`, a
#: library its tests import in `lib/`, and files no rule holds.
PROJECT = {
    "pyproject.toml": ('[tool.pytest.ini_options]\n'
                       '[tool.invective]\nexclude = ["var/*"]\n'),
    "README.md": "read me\n",
    "src/pkg/__init__.py": "",
    "src/pkg/mod.py": "X = 1\n",
    "src/pkg/doc.py": "def f():\n    '''>>> f()\n    1\n    '''\n    return 1\n",
    "lib/util.py": "Y = 2\n",
    "docs/conftest.py": "",
    "tests/conftest.py": "",
    "tests/data/d.json": "{}\n",
    "tests/unit/test_a.py": "def test_a():\n    pass\n",
    "tests/integration/test_b.py": "def test_b():\n    pass\n",
}
UNIT = "tests/unit/test_a.py::test_a"
INTEGRATION = "tests/integration/test_b.py::test_b"
DOCTEST = "src/pkg/doc.py::pkg.doc.f"


class _Dist:
    """An installed distribution, as `importlib.metadata` gives one."""

    def __init__(self, name="dep", version="1.0", record="a,b,c\n",
                 direct=None):
        self.metadata = {"Name": name, "Version": version}
        self.files = {"RECORD": record, "direct_url.json": direct}

    def read_text(self, name):
        return self.files.get(name)


class Keyed:
    """`PROJECT` on disk as a copy at *copy*, inside a worktree's top at
    *top* (the copy itself without a ref), and its campaign's key."""

    def __init__(self, base, copy="", files=PROJECT):
        self.top = str(base / "wt")
        self.copy = os.path.join(self.top, copy) if copy else self.top
        write_tree(self.copy, files)
        self.args = {"tests": ["tests/unit/test_a.py"], "options": [],
                     "selected": [UNIT], "exclude": ["var/*"],
                     "imported": ["lib/util.py", "src/pkg/__init__.py"],
                     "target": "src/pkg/mod.py"}

    def write(self, rel, text, where=None):
        write_tree(where or self.copy, {rel: text})

    def key(self, **changed):
        args = {**self.args, **changed}
        return store.campaign_key(store.hash_tree(self.copy, self.top),
                                  self.copy, args["target"], args["tests"],
                                  args["options"], args["selected"],
                                  args["exclude"], args["imported"])


@pytest.fixture
def keyed(tmp_path, monkeypatch):
    monkeypatch.setattr(store.importlib.metadata, "distributions",
                        lambda: [_Dist()])
    for name in ("PYTEST_ADDOPTS", "PYTHONHOME", "CI"):
        monkeypatch.delenv(name, raising=False)
    return Keyed(tmp_path)


def _python(**changed):
    real = {"version": sys.version, "platform": sys.platform,
            "implementation": SimpleNamespace(
                cache_tag=sys.implementation.cache_tag),
            "prefix": sys.prefix, "executable": sys.executable}
    return SimpleNamespace(**{**real, **changed})


#: Each part of a campaign the key holds, and a change to it: given the
#: project, the monkeypatch and the base temporary directory, it changes the
#: part, and gives the keyword arguments to make the key with.
CHANGES = {
    "format": lambda k, mp, base: mp.setattr(store, "FORMAT", 2),
    "invective version": lambda k, mp, base: mp.setattr(
        store.importlib.metadata, "version", lambda name: "9.9"),
    "the target path": lambda k, mp, base: k.args.update(
        target="src/pkg/doc.py"),
    "the target's text": lambda k, mp, base: k.write("src/pkg/mod.py",
                                                     "X = 2\n"),
    "the tests": lambda k, mp, base: k.args.update(tests=["tests/unit"]),
    "an option": lambda k, mp, base: k.args.update(options=["-p", "no:x"]),
    "the selection's order": lambda k, mp, base: k.args.update(
        selected=[INTEGRATION, UNIT]),
    "a selected test file": lambda k, mp, base: k.write(
        "tests/unit/test_a.py", "def test_a():\n    assert 1\n"),
    "a data file beside it": lambda k, mp, base: k.write(
        "tests/unit/data.txt", "x\n"),
    "tests/data read by tests/unit and tests/integration": lambda k, mp, base: (
        k.args.update(selected=[UNIT, INTEGRATION]),
        k.write("tests/data/d.json", "{\"a\": 1}\n")),
    "tests/data with tests/unit and a doctest in src/pkg": lambda k, mp, base: (
        k.args.update(selected=[UNIT, DOCTEST]),
        k.write("tests/data/d.json", "{\"a\": 1}\n")),
    "a conftest.py": lambda k, mp, base: k.write("docs/conftest.py", "x = 1\n"),
    "a settings file": lambda k, mp, base: k.write("tox.ini",
                                                   "[pytest]\naddopts = -q\n"),
    "pyproject outside tool.invective": lambda k, mp, base: k.write(
        "pyproject.toml", PROJECT["pyproject.toml"].replace(
            "[tool.pytest.ini_options]\n",
            "[tool.pytest.ini_options]\naddopts = '-q'\n")),
    "exclude": lambda k, mp, base: k.args.update(exclude=["var/*", "x"]),
    "exclude in pyproject": lambda k, mp, base: k.write(
        "pyproject.toml", PROJECT["pyproject.toml"].replace('"var/*"', '"x/*"')),
    "an imported module": lambda k, mp, base: k.write("lib/util.py",
                                                      "Y = 3\n"),
    "PYTEST_ADDOPTS": lambda k, mp, base: mp.setenv("PYTEST_ADDOPTS", "-x"),
    "PYTHONHOME": lambda k, mp, base: mp.setenv("PYTHONHOME", "/x"),
    "CI": lambda k, mp, base: mp.setenv("CI", "true"),
    "the Python version": lambda k, mp, base: mp.setattr(
        store, "sys", _python(version="0")),
    "the platform": lambda k, mp, base: mp.setattr(
        store, "sys", _python(platform="other")),
    "the cache tag": lambda k, mp, base: mp.setattr(
        store, "sys", _python(implementation=SimpleNamespace(cache_tag="x"))),
    "the prefix": lambda k, mp, base: mp.setattr(
        store, "sys", _python(prefix="/elsewhere")),
    "the executable": lambda k, mp, base: mp.setattr(
        store, "sys", _python(executable="/elsewhere/python")),
    "a distribution's version": lambda k, mp, base: mp.setattr(
        store.importlib.metadata, "distributions",
        lambda: [_Dist(version="1.1")]),
    "a distribution's RECORD": lambda k, mp, base: mp.setattr(
        store.importlib.metadata, "distributions",
        lambda: [_Dist(record="a,b,d\n")]),
    "a distribution's direct_url.json": lambda k, mp, base: mp.setattr(
        store.importlib.metadata, "distributions",
        lambda: [_Dist(direct='{"url": "x"}')]),
    "a sitecustomize": lambda k, mp, base: (
        write_tree(str(base / "site"), {"sitecustomize.py": "x = 2\n"})),
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_the_campaign_key_changes_with_each_part(keyed, monkeypatch,
                                                 tmp_path, change):
    """A verdict kept under one key is read only by a campaign that is the
    same in each of these: one that differs in any could decide another
    verdict."""
    write_tree(str(tmp_path / "site"), {"sitecustomize.py": "x = 1\n"})
    custom = os.path.join(str(tmp_path / "site"), "sitecustomize.py")
    keyed.args["imported"].append(custom)
    before = keyed.key()
    CHANGES[change](keyed, monkeypatch, tmp_path)
    assert keyed.key() != before


def test_the_engine_s_code_is_in_the_key(keyed, monkeypatch, tmp_path):
    """Not its version alone: an engine edited at one version is another."""
    plugin = tmp_path / "plugin" / "pytest_invective"
    write_tree(str(plugin), {"__init__.py": "", "data.txt": "x"})
    monkeypatch.setattr(store, "pytest_invective", SimpleNamespace(
        __file__=str(plugin / "__init__.py")))
    before = keyed.key()
    write_tree(str(plugin), {"data.txt": "y"})
    assert keyed.key() == before
    write_tree(str(plugin), {"__init__.py": "x = 1\n"})
    assert keyed.key() != before


@pytest.mark.parametrize("where", ["conftest.py", "pytest.ini"])
def test_a_file_above_the_project_in_a_ref_worktree_is_in_the_key(
        tmp_path, monkeypatch, where):
    """Under `--ref`, pytest reads a conftest or settings up to the top of
    the worktree, which the copy, the project's own directory in it, does
    not hold."""
    monkeypatch.setattr(store.importlib.metadata, "distributions", list)
    keyed = Keyed(tmp_path, copy="sub")
    before = keyed.key()
    keyed.write(where, "[pytest]\n" if where == "pytest.ini" else "",
                where=keyed.top)
    assert keyed.key() != before


def test_an_imported_file_above_the_project_in_a_ref_worktree_is_keyed_from_the_worktree(
        tmp_path, monkeypatch):
    """Each run's worktree is in a temporary directory of its own: a file
    there that the copy does not hold is named from the worktree's top, so
    that the next run's key is the same."""
    monkeypatch.setattr(store.importlib.metadata, "distributions", list)
    keys = []
    for run in ("one", "two"):
        keyed = Keyed(tmp_path / run, copy="sub")
        keyed.write("shared/x.py", "Z = 1\n", where=keyed.top)
        imported = [os.path.join(keyed.top, "shared", "x.py")]
        assert store.Tree(keyed.top, {}, {}).key(imported[0]) == "shared/x.py"
        keys.append(keyed.key(imported=imported))
    assert keys[0] == keys[1]


#: What the key leaves out, and a change to it.
IGNORED = {
    "README.md": lambda k, mp: k.write("README.md", "read me again\n"),
    "a comment in pyproject": lambda k, mp: k.write(
        "pyproject.toml", "# a comment\n" + PROJECT["pyproject.toml"]),
    "PYTEST_CURRENT_TEST": lambda k, mp: mp.setenv("PYTEST_CURRENT_TEST", "x"),
    "PYTEST_VERSION": lambda k, mp: mp.setenv("PYTEST_VERSION", "1"),
    "PYTEST_XDIST_WORKER": lambda k, mp: mp.setenv("PYTEST_XDIST_WORKER", "gw9"),
    "an unrelated variable": lambda k, mp: mp.setenv("INVECTIVE_UNRELATED", "1"),
    "the owner's pid": lambda k, mp: k.write(tree.MARKER, '{"pid": 2}'),
    "a file in tree.SKIPPED": lambda k, mp: (
        k.write("__pycache__/x.pyc", "x"), k.write(".git", "gitdir: x")),
    "a virtual environment": lambda k, mp: k.write("venv/pyvenv.cfg", "x"),
}
for _name in ("workers", "confirm", "history", "coverage", "cache",
              "unsafe-speedups", "fail-on-survivors", "max-accepted"):
    IGNORED["[tool.invective] " + _name] = (
        lambda k, mp, name=_name: k.write("pyproject.toml", PROJECT[
            "pyproject.toml"] + "%s = %s\n" % (
                name, "2" if name == "max-accepted" else "true")))


@pytest.mark.parametrize("change", sorted(IGNORED))
def test_the_campaign_key_ignores(keyed, monkeypatch, change):
    """None of these changes a verdict, or each is kept beside it: a change
    that started a fresh campaign would only cost time."""
    keyed.write(tree.MARKER, '{"pid": 1}')
    before = keyed.key()
    IGNORED[change](keyed, monkeypatch)
    assert keyed.key() == before


def test_a_pyproject_with_a_date_hashes_and_the_date_counts(keyed):
    """TOML has dates and times, and JSON has no word for them."""
    keyed.write("pyproject.toml", PROJECT["pyproject.toml"]
                + "[tool.x]\nd = 2026-10-10\nt = 10:00:00\n")
    before = keyed.key()
    keyed.write("pyproject.toml", PROJECT["pyproject.toml"]
                + "[tool.x]\nd = 2026-10-11\nt = 10:00:00\n")
    assert keyed.key() != before


def test_a_pyproject_that_is_not_toml_is_hashed_as_it_is(keyed):
    keyed.write("pyproject.toml", "[x\n")
    before = keyed.key()
    keyed.write("pyproject.toml", "[x \n")
    assert keyed.key() != before


def test_the_settings_rule_wins_over_the_tests_directory_rule(keyed):
    """A `pyproject.toml` beside the tests is pytest's settings, read as
    data: a comment in it leaves the key as it was."""
    keyed.write("tests/pyproject.toml", "[tool.pytest.ini_options]\n")
    before = keyed.key()
    keyed.write("tests/pyproject.toml", "# a comment\n"
                "[tool.pytest.ini_options]\n")
    assert keyed.key() == before
    keyed.write("tests/pyproject.toml", "[tool.pytest.ini_options]\n"
                "addopts = '-q'\n")
    assert keyed.key() != before


def test_a_selected_file_at_the_top_is_hashed_alone(keyed):
    keyed.write("test_top.py", "def test_t():\n    pass\n")
    before = keyed.key(selected=["test_top.py::test_t"])
    keyed.write("tests/unit/test_a.py", "def test_a():\n    assert 1\n")
    assert keyed.key(selected=["test_top.py::test_t"]) == before
    keyed.write("test_top.py", "def test_t():\n    assert 1\n")
    assert keyed.key(selected=["test_top.py::test_t"]) != before


def test_the_c_file_of_the_options_is_in_the_key(keyed, tmp_path):
    """pytest's settings named with `-c`, inside the project or not."""
    for given in ("given.ini", str(tmp_path / "out" / "pytest.ini")):
        write_tree(str(tmp_path / "out"), {"pytest.ini": "[pytest]\n"})
        keyed.write("given.ini", "[pytest]\n")
        before = keyed.key(options=["-c", given])
        write_tree(str(tmp_path / "out"), {"pytest.ini": "[pytest]\nx = 1\n"})
        keyed.write("given.ini", "[pytest]\nx = 1\n")
        assert keyed.key(options=["-c", given]) != before


def test_a_zip_import_is_hashed_as_the_archive(keyed):
    """A module read from a zip on `sys.path` names a file inside it: the
    archive is what changes. A module whose file is gone, its directory
    there, hashes as absent."""
    keyed.write("lib/deps.zip", "zip one")
    inside = ["lib/deps.zip/dep/mod.py"]
    before = keyed.key(imported=inside)
    keyed.write("lib/deps.zip", "zip two")
    assert keyed.key(imported=inside) != before
    assert store._import_digest(os.path.join(keyed.copy, "lib",
                                             "gone.py")) == store.ABSENT


def test_a_file_the_baseline_wrote_is_not_in_the_map_and_a_generated_import_is_hashed(
        keyed):
    """The copy is hashed before its first run, and a file a run writes
    there is not in it; a module the baseline generated and imported is
    hashed as it stands."""
    hashed = store.hash_tree(keyed.copy, keyed.top)
    keyed.write("lib/generated.py", "G = 1\n")
    assert "lib/generated.py" not in hashed.files

    def key():
        return store.campaign_key(hashed, keyed.copy, "src/pkg/mod.py",
                                  keyed.args["tests"], [], [UNIT], [],
                                  ["lib/generated.py"])

    before = key()
    keyed.write("lib/generated.py", "G = 2\n")
    assert key() != before


def test_a_link_is_hashed_by_what_it_reads_or_by_its_text(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("no links")
    write_tree(str(tmp_path / "copy"), {"real.txt": "one"})
    try:
        os.symlink("real.txt", tmp_path / "copy" / "link.txt")
        os.symlink("nowhere", tmp_path / "copy" / "broken.txt")
        os.symlink(str(tmp_path), tmp_path / "copy" / "up")
    except OSError:
        pytest.skip("this user cannot make links")
    hashed = store.hash_tree(str(tmp_path / "copy"), str(tmp_path / "copy"))
    assert hashed.files["link.txt"] == hashed.files["real.txt"]
    assert hashed.files["broken.txt"] != hashed.files["up"]
    assert not any(name.startswith("up/") for name in hashed.files)


def test_a_file_that_cannot_be_read_is_an_error(tmp_path, monkeypatch):
    write_tree(str(tmp_path / "copy"), {"a.txt": "x"})

    def unreadable(*args, **kwargs):
        raise PermissionError("no")

    monkeypatch.setattr(store.hashlib, "file_digest", unreadable)
    with pytest.raises(OSError):
        store.hash_tree(str(tmp_path / "copy"), str(tmp_path / "copy"))


# --------------------------------------------------------------------------
# The verdict cache: what is kept, and what is read


@pytest.mark.parametrize("ok, code, killer, kept", [
    (True, 0, "", True),
    (False, 1, "t.py::test_a", True),
    (False, 2, "t.py", True),
    (False, 1, "", False),
    (False, 2, "", False),
    (False, 3, "t.py::test_a", False),
    (False, 4, "t.py", False),
    (False, 5, "t.py", False),
    (False, -1, "TIMEOUT", False),
    (False, -9, "", False),
    (False, -9, "t.py::test_a", False),
    (True, 1, "", False),
    (True, 0, "t.py::test_a", False)])
def test_sound_keeps_a_survivor_and_a_kill_by_a_test_or_a_module_and_nothing_else(
        ok, code, killer, kept):
    """A kill by time, a signal, a crash or the harness can be the machine's
    load or memory: read back, it would be a kill no run made."""
    assert store.sound(ok, code, killer) is kept


KEY = "c" * 64
TEXT = "the mutant's text\n"


def _entry(ok=False, origin="", concurrent=False, safe=False, **changed):
    entry = {"campaign": KEY, "mutant": store._hex(TEXT), "ok": ok,
             "code": 0 if ok else 1, "killer": "" if ok else "t.py::test_a",
             "origin": origin, "concurrent": concurrent, "safe": safe}
    return {**entry, **changed}


def _cache(tmp_path, count=1, confirm=False, safe_only=False, origins=("",),
           say=print):
    return store.Cache(str(tmp_path), KEY, count, confirm, target=TARGET,
                       say=say, safe_only=safe_only, origins=origins)


def _kept(tmp_path, entry, **state):
    """*entry* written as the cache's file for `TEXT`, and the cache of a run
    in *state*: what it reads for `TEXT`, and how many entries it counts as
    bad."""
    cache = _cache(tmp_path, **state)
    os.makedirs(cache.where, exist_ok=True)
    with open(cache._path(store._hex(TEXT)), "w", encoding="utf-8") as fh:
        fh.write(entry if isinstance(entry, str) else json.dumps(entry))
    return cache.get(TEXT), len(cache.bad)


#: Every entry a run can write: (ok, origin, concurrent, safe).
REACHABLE = ([(ok, "", concurrent, safe) for ok in (True, False)
              for concurrent, safe in ((False, False), (True, False),
                                       (False, True))]
             + [(False, origin, concurrent, False)
                for origin in ("probe", "coverage")
                for concurrent in (False, True)])
#: Every run the engine makes, as the cache sees it: (the switch, workers,
#: confirm, history in use, coverage in use).
STATES = ([(True, 1, confirm, False, False) for confirm in (False, True)]
          + [(False, count, confirm, history, coverage)
             for count in (1, 2) for confirm in (False, True)
             for history in (False, True) for coverage in (False, True)])


def _state(switch, count, confirm, history, coverage):
    return {"count": count, "confirm": confirm, "safe_only": switch,
            "origins": [""] + ["probe"] * history + ["coverage"] * coverage}


def _owner_says(shape, state):
    """The read rule, as the owner put it."""
    ok, origin, concurrent, safe = shape
    switch, count, confirm, history, coverage = state
    in_use = {"": True, "probe": history, "coverage": coverage}
    return ((not switch or safe)
            and not (not ok and confirm and count > 1)
            and not (concurrent and count == 1)
            and in_use[origin])


def test_the_read_rule(tmp_path):
    """Each entry a run can write against each run the engine can make: with
    the switch on, only what was decided with it on; no kill with `confirm`
    at more than one worker; nothing measured beside other copies at one
    worker; and a kill a probe or a narrowed selection made only while that
    is in use."""
    assert len(REACHABLE) == 10 and len(STATES) == 18
    wrong = []
    for shape in REACHABLE:
        ok, origin, concurrent, safe = shape
        for state in STATES:
            got, bad = _kept(tmp_path, _entry(ok, origin, concurrent, safe),
                             **_state(*state))
            hit = got is not None
            if hit != _owner_says(shape, state) or bad:
                wrong.append((shape, state, hit, bad))
            if hit:
                assert got == store.Entry(ok, 0 if ok else 1,
                                          "" if ok else "t.py::test_a", origin)
    assert not wrong
    # The oracle hits and misses both.
    said = {_owner_says(shape, state) for shape in REACHABLE for state in STATES}
    assert said == {True, False}


#: The entry shapes no run writes.
UNREACHABLE = [(ok, origin, concurrent, safe)
               for ok in (True, False) for origin in ("", "probe", "coverage")
               for concurrent in (False, True) for safe in (False, True)
               if (ok, origin, concurrent, safe) not in REACHABLE]


@pytest.mark.parametrize("shape", UNREACHABLE)
def test_every_entry_shape_that_cannot_occur_is_a_miss(tmp_path, shape):
    """Hand-edited or foreign: never read, under any run, and counted."""
    assert len(UNREACHABLE) == 14
    for state in STATES:
        assert _kept(tmp_path, _entry(*shape), **_state(*state)) == (None, 1)


@pytest.mark.parametrize("entry", [
    "{", "[]", json.dumps(_entry(campaign="d" * 64)),
    json.dumps(_entry(mutant="0" * 64)),
    json.dumps({**_entry(), "extra": 1}),
    json.dumps({k: v for k, v in _entry().items() if k != "safe"}),
    json.dumps(_entry(ok=1)), json.dumps(_entry(code="1")),
    json.dumps(_entry(code=True)), json.dumps(_entry(killer=None)),
    json.dumps(_entry(origin="other")), json.dumps(_entry(concurrent=0)),
    json.dumps(_entry(safe=None)),
    json.dumps(_entry(ok=True, code=1)),
    json.dumps(_entry(ok=True, killer="t.py::test_a"))],
    ids=["torn", "not-an-object", "campaign", "mutant", "extra", "short",
         "ok", "code", "code-bool", "killer", "origin", "concurrent", "safe",
         "survivor-code", "survivor-killer"])
def test_a_foreign_wrong_shape_or_wrong_hash_entry_is_a_miss_with_one_warning(
        tmp_path, entry):
    assert _kept(tmp_path, entry) == (None, 1)


def test_an_unsound_entry_is_a_miss(tmp_path):
    """A kill by time, kept by no run, is not read and not counted: it has
    an entry's shape."""
    assert _kept(tmp_path, _entry(code=-1, killer="TIMEOUT")) == (None, 0)
    assert _kept(tmp_path, _entry(killer="")) == (None, 0)


def test_an_absent_entry_is_a_miss_without_a_warning(tmp_path):
    cache = _cache(tmp_path)
    assert cache.get(TEXT) is None and not cache.bad
    assert not os.path.exists(tmp_path / ".invective")


@pytest.mark.parametrize("count, origins", [(2, ("",)), (1, ("", "probe")),
                                            (1, ("coverage",))])
def test_a_cache_built_for_a_state_the_engine_never_passes_raises(
        tmp_path, count, origins):
    """The switch runs one worker and no attempt; a cache told otherwise
    is a bug, not a run."""
    with pytest.raises(ValueError):
        _cache(tmp_path, count=count, safe_only=True, origins=origins)
    _cache(tmp_path, count=count, origins=origins)


# --------------------------------------------------------------------------
# The verdict cache: what is written


def _entries(cache):
    found = {}
    for name in sorted(os.listdir(cache.where)):
        with open(os.path.join(cache.where, name), encoding="utf-8") as fh:
            found[name] = json.load(fh)
    return found


@pytest.mark.parametrize("count, via, confirmed, origin, concurrent", [
    (1, "", "", "", False), (2, "", "", "", True),
    (2, "probe", "", "probe", True), (1, "coverage", "", "coverage", False),
    (2, "coverage", "alone", "coverage", False), (2, "", "full", "", False)])
def test_a_put_writes_what_landed_and_how(tmp_path, count, via, confirmed,
                                          origin, concurrent):
    cache = _cache(tmp_path, count=count, origins=("", "probe", "coverage"))
    cache.put(TEXT, False, 1, "t.py::test_a", via, confirmed)
    (name, entry), = _entries(cache).items()
    assert name == store._hex(TEXT)[:32] + ".json"
    assert entry == _entry(origin=origin, concurrent=concurrent)
    assert cache.where == os.path.join(
        str(tmp_path), ".invective", "cache", store._hex(TARGET)[:12],
        KEY[:24])
    with open(tmp_path / ".invective" / ".gitignore", encoding="utf-8") as fh:
        assert fh.read() == store.IGNORE


def test_a_put_with_the_switch_on_is_safe(tmp_path):
    cache = _cache(tmp_path, safe_only=True)
    cache.put(TEXT, True, 0, "", "", "")
    assert list(_entries(cache).values()) == [_entry(ok=True, safe=True)]
    assert cache.get(TEXT) == store.Entry(True, 0, "", "")


@pytest.mark.parametrize("ok, code, killer", [
    (False, -1, "TIMEOUT"), (False, -9, ""), (False, 3, "t.py::test_a"),
    (False, 1, "")])
def test_an_unsound_verdict_is_not_put(tmp_path, ok, code, killer):
    cache = _cache(tmp_path)
    cache.put(TEXT, ok, code, killer, "", "")
    assert not os.path.exists(cache.where)


def test_an_entry_write_cut_short_leaves_no_entry(tmp_path, monkeypatch):
    """A ^C between the temporary and its place: no entry, no temporary.
    One torn by hand is a miss."""
    cache = _cache(tmp_path)

    def interrupted(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(store.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cache.put(TEXT, True, 0, "", "", "")
    assert os.listdir(cache.where) == []
    monkeypatch.undo()
    assert _kept(tmp_path, json.dumps(_entry())[:-3]) == (None, 1)


def test_old_temporaries_are_swept_once(tmp_path, monkeypatch):
    """At the first put of a run, not at every one."""
    swept = []
    real = store.sweep_temporaries
    monkeypatch.setattr(store, "sweep_temporaries",
                        lambda where: (swept.append(where), real(where)))
    cache = _cache(tmp_path)
    os.makedirs(cache.where)
    old = os.path.join(cache.where, "tmpold.json")
    with open(old, "w", encoding="utf-8") as fh:
        fh.write("{")
    os.utime(old, (1, 1))
    cache.put(TEXT, True, 0, "", "", "")
    cache.put(TEXT + "x", True, 0, "", "", "")
    assert swept == [cache.where]
    assert not os.path.exists(old)


def test_a_failing_put_warns_once_and_stops_putting(tmp_path, monkeypatch):
    lines, say = _said()
    cache = _cache(tmp_path, say=say)
    tried = []

    def full(src, dst):
        tried.append(dst)
        raise OSError("the disk is full")

    monkeypatch.setattr(store.os, "replace", full)
    cache.put(TEXT, True, 0, "", "", "")
    cache.put(TEXT + "x", True, 0, "", "", "")
    assert len(tried) == 1
    (line,) = lines
    assert line.startswith("warning:   ") and "the disk is full" in line
    assert os.listdir(cache.where) == []


def test_a_cache_directory_that_cannot_be_made_warns_once(tmp_path):
    (tmp_path / ".invective").write_text("a file", encoding="utf-8")
    lines, say = _said()
    cache = _cache(tmp_path, say=say)
    cache.put(TEXT, True, 0, "", "", "")
    cache.put(TEXT + "x", True, 0, "", "", "")
    (line,) = lines
    assert line.startswith("warning:   %s could not be made, so no verdict "
                           "is kept: " % cache.where)


def test_on_record_counts_entries_not_temporaries(tmp_path):
    cache = _cache(tmp_path)
    assert cache.on_record() == 0
    cache.put(TEXT, True, 0, "", "", "")
    cache.put(TEXT + "x", True, 0, "", "", "")
    for name in ("tmp0123456789abcdef0123456789abcdef.json", "notes.txt",
                 "0123456789abcdef0123456789abcdef.json.bak",
                 "0123456789ABCDEF0123456789ABCDEF.json"):
        (tmp_path / cache.where / name).write_text("{}", encoding="utf-8")
    assert cache.on_record() == 2


def test_campaigns_beyond_the_current_and_three_newest_are_pruned(tmp_path):
    """Of this target's campaigns, the current one and the three newest
    others stay; another target's and the `.gitignore` are not touched. A
    campaign removed under a put is made again."""
    cache = _cache(tmp_path)
    cache.put(TEXT, True, 0, "", "", "")
    other = store.Cache(str(tmp_path), "e" * 64, 1, False, target="other.py",
                        say=print)
    other.put(TEXT, True, 0, "", "", "")
    os.utime(other.where, (1, 1))
    ages = {}
    for age, name in enumerate("0123456"):
        where = os.path.join(cache.campaigns, name * 24)
        os.makedirs(where)
        os.utime(where, (1000 + age, 1000 + age))
        ages[name * 24] = age
    os.utime(cache.where, (1, 1))
    cache.finish()
    assert sorted(os.listdir(cache.campaigns)) == sorted(
        [KEY[:24], "4" * 24, "5" * 24, "6" * 24])
    assert os.path.getmtime(cache.where) > 1000
    assert os.listdir(other.campaigns) == ["e" * 24]
    assert sorted(os.listdir(tmp_path / ".invective")) == [".gitignore",
                                                           "cache"]
    shutil.rmtree(cache.where)
    cache.put(TEXT + "x", True, 0, "", "", "")
    assert cache.on_record() == 1


def test_finishing_a_campaign_that_kept_nothing_is_fine(tmp_path):
    _cache(tmp_path).finish()
    assert not os.path.exists(tmp_path / ".invective")
