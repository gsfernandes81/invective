"""What invective keeps in a project between runs: the `.invective/`
directory, its files written whole or not at all, and the killer history."""

from __future__ import annotations

import ast
import json
import os
import stat

import pytest

from invective import mutate, store

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
