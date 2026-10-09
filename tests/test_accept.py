"""Acceptances in the source, the `[tool.invective]` settings and the project
they belong to, and the gate."""

from __future__ import annotations

import os

import pytest

from invective import accept
from invective import config
from invective import mutate
from invective.config import Config
from invective.errors import Refusal

from conftest import no_pytest_settings_above, write_tree


def test_an_acceptance_on_the_line_applies_to_that_line():
    (got,) = accept.read(
        "x = out[-400:]  # invective: accept[equivalent] any length serves\n",
        "m.py")
    assert got == accept.Accept(1, 1, "equivalent", None, "any length serves")


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_an_acceptance_is_on_the_line_the_parser_counts_whatever_ends_it(ending):
    """The source is read with its own endings, and a lone `\\r` is a line
    break to the tokenizer; read on `\\n` alone, every comment of such a
    file would stand on line 1."""
    source = "x = 1\ny = 2\nz = 3  # invective: accept[equivalent] why\n"
    (got,) = accept.read(source.replace("\n", ending), "m.py")
    assert (got.line, got.at) == (3, 3)


def test_acceptances_above_a_line_apply_to_it_however_many_stand_there():
    """pytest-gremlins' line-above form, stacked."""
    source = ("# invective: accept[equivalent: 400 -> 401] any length\n"
              "# invective: accept[untestable] Windows only\n"
              "x = f(out[-400:], True)\n")
    got = accept.read(source, "m.py")
    assert [(a.line, a.at, a.change) for a in got] == [
        (3, 1, "400 -> 401"), (3, 2, None)]


def test_an_acceptance_above_a_blank_line_applies_to_nothing():
    """It is not carried down to the next code: it becomes stale instead."""
    (got,) = accept.read("# invective: accept[equivalent] why\n\nx = 1\n", "m.py")
    assert got.line == 2


def test_a_mark_inside_a_string_is_not_a_comment():
    assert accept.read('x = "# invective: accept[nonsense]"\n', "m.py") == []


def test_an_ordinary_comment_is_left_alone():
    assert accept.read("x = 1  # a comment of the author's\n", "m.py") == []


def test_a_source_that_cannot_be_read_for_comments_is_refused():
    with pytest.raises(Refusal) as caught:
        accept.read("x = (\n", "m.py")
    assert "cannot be read for its comments" in str(caught.value)


def test_one_mutant_of_a_line_can_be_named():
    (got,) = accept.read(
        "x = 3 < y  # invective: accept[equivalent: 3 -> 4] why\n", "m.py")
    assert got.covers(1, "3 -> 4")
    assert not got.covers(1, "Lt -> LtE")
    assert not got.covers(2, "3 -> 4")


@pytest.mark.parametrize("comment, says", [
    ("# invective: accept[maybe] why", "no reason to accept"),
    ("# invective: accept[equivalent]", "says why"),
    ("# invective: accept[equivalent:] why", "names the one mutant"),
    ("# invective: pardon[equivalent] why", "accept[reason] why"),
])
def test_a_malformed_acceptance_is_refused_where_it_stands(comment, says):
    with pytest.raises(Refusal) as caught:
        accept.read("x = 1\n" + comment + "\n", "pkg/m.py")
    assert str(caught.value).startswith("pkg/m.py:2: ")
    assert says in str(caught.value)


def test_the_settings_are_read_from_the_project_s_pyproject(tmp_path):
    write_tree(str(tmp_path), {"pyproject.toml": (
        "[tool.invective]\n"
        "fail-on-survivors = true\n"
        "max-accepted = 3\n"
        "exclude = ['var/*']\n")})
    assert config.load(str(tmp_path)) == Config(True, 3, ("var/*",))


@pytest.mark.parametrize("text", ["", "[tool.invective]\nexclude = []\n"])
def test_what_is_not_set_is_the_default(tmp_path, text):
    write_tree(str(tmp_path), {"pyproject.toml": text})
    assert config.load(str(tmp_path)) == Config()


def test_without_a_pyproject_the_settings_are_the_defaults(tmp_path):
    assert config.load(str(tmp_path)) == Config()


@pytest.mark.parametrize("table, says", [
    ("fail-on-survivor = true", "no setting 'fail-on-survivor'"),
    ("fail-on-survivors = 1", "must be true or false"),
    ("max-accepted = true", "must be a whole number"),
    ("exclude = [1]", "list of strings"),
    ("exclude = [", "not valid TOML"),
])
def test_a_setting_that_cannot_be_right_is_refused(tmp_path, table, says):
    """A misspelt key would otherwise read as absent, and the gate as off."""
    write_tree(str(tmp_path), {"pyproject.toml": "[tool.invective]\n" + table})
    with pytest.raises(Refusal) as caught:
        config.load(str(tmp_path))
    assert says in str(caught.value)


def test_confirm_is_read_from_the_settings(tmp_path):
    write_tree(str(tmp_path), {"pyproject.toml": (
        "[tool.invective]\nconfirm = true\n")})
    assert config.load(str(tmp_path)) == Config(confirm=True)
    write_tree(str(tmp_path), {"pyproject.toml": (
        "[tool.invective]\nconfirm = 1\n")})
    with pytest.raises(Refusal) as caught:
        config.load(str(tmp_path))
    assert "confirm must be true or false" in str(caught.value)


@pytest.mark.parametrize("value, setting", [("3", 3), ('"auto"', "auto")])
def test_workers_is_a_count_or_auto(tmp_path, value, setting):
    write_tree(str(tmp_path), {"pyproject.toml": (
        "[tool.invective]\nworkers = %s\n" % value)})
    assert config.load(str(tmp_path)) == Config(workers=setting)


@pytest.mark.parametrize("value, said", [
    ("true", "True"), ("0", "0"), ("-1", "-1"), ('"autos"', "'autos'"),
    ("2.0", "2.0"), ("[2]", "[2]")])
def test_a_count_of_workers_that_cannot_be_right_is_refused_by_name(
        tmp_path, value, said):
    write_tree(str(tmp_path), {"pyproject.toml": (
        "[tool.invective]\nworkers = %s\n" % value)})
    with pytest.raises(Refusal) as caught:
        config.load(str(tmp_path))
    assert str(caught.value) == (
        '[tool.invective] workers must be a whole number above 0 or "auto", '
        "not %s" % said)


@pytest.mark.parametrize("value, count", [(1, 1), (12, 12), ("12", 12),
                                          ("auto", 6)])
def test_a_count_of_workers_is_taken_as_given_or_as_its_digits(
        monkeypatch, value, count):
    monkeypatch.setattr(config, "_cpus", lambda: 6)
    assert config.workers(value) == count
    # A cap on the mutants there are applies to every count.
    assert config.workers(value, most=2) == min(count, 2)
    assert config.workers(value, most=0) == 1


def test_auto_is_at_most_AUTO_MOST(monkeypatch):
    monkeypatch.setattr(config, "_cpus", lambda: 64)
    assert config.workers("auto") == config.AUTO_MOST == 8
    assert config.workers("auto", most=50) == 8
    assert config.workers("auto", most=0) == 1


@pytest.mark.parametrize("quota, cpus", [
    (None, 6), ("max 100000\n", 6), ("200000 100000\n", 2),
    ("250000 100000\n", 2), ("50000 100000\n", 1), ("garbled\n", 6)])
def test_auto_counts_the_cpus_this_process_may_use_and_its_quota(
        tmp_path, monkeypatch, quota, cpus):
    """A container given two CPUs of a host's sixty-four sees every one of
    them, and a worker for each would only queue for the two."""
    cpu_max = tmp_path / "cpu.max"
    if quota is not None:
        cpu_max.write_text(quota, encoding="ascii")
    monkeypatch.setattr(config, "_CPU_MAX", str(cpu_max))
    monkeypatch.setattr(config.os, "process_cpu_count", lambda: 6,
                        raising=False)
    assert config._cpus() == cpus


def test_without_a_count_of_its_own_cpus_auto_asks_the_affinity(monkeypatch,
                                                                tmp_path):
    monkeypatch.setattr(config, "_CPU_MAX", str(tmp_path / "none"))
    monkeypatch.delattr(config.os, "process_cpu_count", raising=False)
    monkeypatch.setattr(config.os, "sched_getaffinity", lambda pid: {0, 1, 2},
                        raising=False)
    assert config._cpus() == 3
    monkeypatch.delattr(config.os, "sched_getaffinity")
    monkeypatch.setattr(config.os, "cpu_count", lambda: 5)
    assert config._cpus() == 5
    monkeypatch.setattr(config.os, "cpu_count", lambda: None)
    assert config._cpus() == 1


def _marker_above(path):
    """A marker of a project's top in a directory above *path*, or None."""
    here = os.path.dirname(path)
    while True:
        for name in config._PROJECT + config._REPOSITORY:
            if os.path.exists(os.path.join(here, name)):
                return os.path.join(here, name)
        up = os.path.dirname(here)
        if up == here:
            return None
        here = up


def test_the_project_s_top_is_the_nearest_pyproject_above_the_working_directory(
        tmp_path):
    """From a subdirectory the whole project is the one copied, and its
    settings the ones read; with no marker anywhere, where the command was
    started is the top."""
    top = os.path.realpath(tmp_path)
    marker = _marker_above(top)
    if marker is not None:
        pytest.skip("%s is above the temporary directory, so no directory "
                    "in it is without a marker above" % marker)
    deep = os.path.join(top, "a", "b", "c")
    os.makedirs(deep)
    assert config.project_root(deep) == deep

    write_tree(top, {"a/pyproject.toml": ""})
    assert config.project_root(deep) == os.path.join(top, "a")

    # The nearest: a project inside another is the top from inside it.
    write_tree(top, {"a/b/pyproject.toml": ""})
    assert config.project_root(deep) == os.path.join(top, "a", "b")


@pytest.mark.parametrize("name", [".git", ".hg", ".svn"])
@pytest.mark.parametrize("as_file", [True, False])
def test_a_repository_s_directory_ends_the_walk_and_is_no_top_by_itself(
        tmp_path, name, as_file):
    """A `pyproject.toml` of a tool's settings above a repository never makes
    that directory the project, and a project with no marker is the
    directory the command was started in, where its tests import it.
    `.git` is a file in a worktree."""
    home = os.path.realpath(tmp_path)
    proj = os.path.join(home, "proj")
    pkg = os.path.join(proj, "pkg")
    write_tree(home, {"pyproject.toml": "", "proj/pkg/__init__.py": ""})
    if as_file:
        write_tree(home, {"proj/" + name: "gitdir: elsewhere\n"})
    else:
        os.mkdir(os.path.join(proj, name))
    assert config.project_root(pkg) == pkg
    assert config.project_root(proj) == proj

    write_tree(home, {"proj/pyproject.toml": ""})
    assert config.project_root(os.path.join(proj, "pkg")) == proj


@pytest.mark.parametrize("marker", ["pytest.toml", ".pytest.toml",
                                    "pytest.ini", ".pytest.ini", "tox.ini",
                                    "setup.cfg", "setup.py"])
def test_a_file_pytest_takes_for_a_project_s_marks_its_top(tmp_path, marker):
    """A project with no `pyproject.toml` and no repository, under a home
    directory whose `pyproject.toml` holds a tool's settings, is its own top:
    the home directory is not copied, and its secrets with it."""
    home = os.path.realpath(tmp_path)
    proj = os.path.join(home, "proj")
    write_tree(home, {"pyproject.toml": "", "proj/" + marker: "",
                      "proj/pkg/__init__.py": ""})
    assert config.project_root(os.path.join(proj, "pkg")) == proj


@pytest.mark.parametrize("marker", ["pyproject.toml", ".git", "../.git"])
def test_the_home_directory_is_the_top_only_from_itself(tmp_path,
                                                       monkeypatch, marker):
    """A `pyproject.toml` of a tool's settings in the home directory, a
    dotfiles repository there, or a marker above it, does not make it or
    anything above it the top of a project below it with no marker of its
    own: that project's top is where the command was started, and the home
    directory is not copied. A dotfiles repository above the home directory
    ends the walk at the home directory itself."""
    home = os.path.join(os.path.realpath(tmp_path), "home")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    proj = os.path.join(home, "proj")
    write_tree(home, {marker: "[tool.ruff]\n", "proj/gate.py": ""})
    assert config.project_root(proj) == proj
    assert config.project_root(home) == home

    write_tree(home, {"proj/pytest.ini": "", "proj/pkg/__init__.py": ""})
    assert config.project_root(os.path.join(proj, "pkg")) == proj


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="a symbolic link without privileges")
def test_the_home_directory_reached_through_a_link_is_still_the_stop(
        tmp_path, monkeypatch):
    """`HOME` spelt through a link to the home directory, while the working
    directory is the physical path, stops the walk at the home directory all
    the same: the project below it is its own top and the home directory is
    not copied."""
    real = os.path.realpath(tmp_path / "real")
    home = os.path.join(real, "home")
    proj = os.path.join(home, "proj")
    write_tree(real, {"home/pyproject.toml": "[tool.ruff]\n",
                      "home/proj/gate.py": ""})
    os.symlink(real, tmp_path / "linked")
    linked_home = str(tmp_path / "linked" / "home")
    monkeypatch.setenv("HOME", linked_home)
    monkeypatch.setenv("USERPROFILE", linked_home)
    assert config.project_root(proj) == proj
    assert config.project_root(home) == home


_STRICT = "[pytest]\nxfail_strict = true\n"
_MARKERS = "[pytest]\nmarkers =\n    slow: a slow test\n"
#: A repository `w` and the tree of one of its refs `t`, each with the
#: project `sub` in it, whose own `pyproject.toml` sets nothing. A `.git`
#: file marks each one's top, as it does a worktree's.
_REF = {"w/.git": "gitdir: elsewhere\n", "w/sub/pyproject.toml": "[project]\n",
        "t/.git": "gitdir: elsewhere\n", "t/sub/pyproject.toml": "[project]\n"}
_TESTS = {"m/.git": "", "m/tests/pytest.ini": "", "m/tests/test_a.py": "",
          "m/tests/sub/test_b.py": "", "pytest.ini": _STRICT}


#: (files, the project, the tree its runs are in or None for the project
#: itself, the runs' arguments, whether the tree is a ref's, the file named
#: in the refusal or None for none).
_SEARCHES = {
    "settings-at-the-top": ({"p/pytest.ini": _STRICT}, "p", None, [], False,
                            None),
    "a-monorepo-s-settings-above": (
        {"pytest.ini": _STRICT, "p/pyproject.toml": "[project]\n"}, "p", None,
        [], False, "pytest.ini"),
    "an-empty-table-in-the-project-stops-it": (
        {"pytest.ini": _STRICT,
         "p/pyproject.toml": "[tool.pytest.ini_options]\n"}, "p", None, [],
        False, None),
    "an-empty-tool-pytest-table-does-not": (
        {"pytest.ini": _STRICT, "a/pyproject.toml": "[tool.pytest]\n",
         "a/m/.git": ""}, "a/m", None, [], False, "pytest.ini"),
    "only-markers-above": (
        {"pytest.ini": _MARKERS, "p/pyproject.toml": "[project]\n"}, "p",
        None, [], False, None),
    "only-markers-in-a-pyproject-above": (
        {"pyproject.toml": "[tool.pytest.ini_options]\nmarkers = ['slow']\n",
         "p/.git": ""}, "p", None, [], False, None),
    "markers-and-more-above": (
        {"pytest.ini": _MARKERS + "xfail_strict = true\n",
         "p/pyproject.toml": "[project]\n"}, "p", None, [], False,
        "pytest.ini"),
    "an-empty-pytest-ini-above": (
        {"pytest.ini": "[pytest]\n", "p/.git": ""}, "p", None, [], False,
        None),
    "an-empty-pytest-ini-stops-it-before-settings-above": (
        {"pytest.ini": _STRICT, "a/pytest.ini": "", "a/m/.git": ""}, "a/m",
        None, [], False, None),
    "a-conftest-from-a-settings-file-s-rootdir-down": (
        {"pytest.ini": "", "conftest.py": "", "w/conftest.py": "",
         "w/p/.git": ""}, "w/p", None, [], False, "w/conftest.py"),
    "a-conftest-at-the-settings-file": (
        {"pytest.ini": "", "conftest.py": "", "w/p/.git": ""}, "w/p", None,
        [], False, "conftest.py"),
    "the-project-s-pyproject-is-pytest-s-fallback": (
        {"pyproject.toml": "[tool.ruff]\n", "conftest.py": "",
         "p/pyproject.toml": "[project]\n"}, "p", None, [], False, None),
    "a-conftest-below-the-nearest-pyproject-above": (
        {"pyproject.toml": "[tool.ruff]\n", "w/conftest.py": "",
         "w/p/.git": ""}, "w/p", None, [], False, "w/conftest.py"),
    "a-conftest-at-the-nearest-pyproject-above": (
        {"w/pyproject.toml": "", "w/conftest.py": "", "w/p/.git": ""}, "w/p",
        None, [], False, "w/conftest.py"),
    "a-conftest-above-the-nearest-pyproject": (
        {"pyproject.toml": "", "conftest.py": "", "w/pyproject.toml": "",
         "w/p/.git": ""}, "w/p", None, [], False, None),
    "a-tool-s-pyproject-above-and-no-conftest": (
        {"pyproject.toml": "[tool.ruff]\n", "w/p/.git": ""}, "w/p", None, [],
        False, None),
    "a-conftest-above-with-no-pyproject-anywhere": (
        {"conftest.py": "", "p/.git": ""}, "p", None, [], False, None),
    "from-the-tests-own-settings": (
        _TESTS, "m", None, ["tests/test_a.py::test_x", "-q", "tests/sub"],
        False, None),
    "from-the-top": (_TESTS, "m", None, [], False, "pytest.ini"),
    "from-a-test-file-s-directory": (
        _TESTS, "m", None, ["tests/test_a.py"], False, None),
    "from-a-test-directory": (_TESTS, "m", None, ["tests/sub"], False, None),
    "a-node-id-starts-it-from-its-file-s-directory": (
        _TESTS, "m", None, ["tests/test_a.py::test_x"], False, None),
    "a-path-that-is-not-there-is-no-start": (
        _TESTS, "m", None, ["tests/test_a.py", "other/none.py"], False, None),
    "a-path-that-names-nothing-or-leads-out": (
        _TESTS, "m", None, ["tests/none.py", ".."], False, "pytest.ini"),
    "a-copy-of-a-project-below-its-repository-s-settings": (
        {**_REF, "w/pytest.ini": _STRICT, "t/pytest.ini": _STRICT}, "w/sub",
        None, [], False, "w/pytest.ini"),
    "a-ref-s-tree-holds-its-repository-s-settings": (
        {**_REF, "w/pytest.ini": _STRICT, "t/pytest.ini": _STRICT}, "w/sub",
        "t/sub", [], True, None),
    "a-ref-s-tree-ends-it-before-settings-above-the-repository": (
        {**_REF, "w/pytest.ini": _STRICT, "t/pytest.ini": _STRICT,
         "pytest.ini": _STRICT}, "w/sub", "t/sub", [], True, None),
    "a-ref-goes-by-its-own-settings-not-the-files-as-they-stand": (
        {**_REF, "w/pytest.ini": _STRICT}, "w/sub", "t/sub", [], True, None),
    "settings-above-a-repository-are-in-no-ref": (
        {**_REF, "pytest.ini": _STRICT}, "w/sub", "t/sub", [], True,
        "pytest.ini"),
    "a-ref-s-own-settings-stop-it-first": (
        {**_REF, "pytest.ini": _STRICT,
         "t/sub/pyproject.toml": "[tool.pytest.ini_options]\n"}, "w/sub",
        "t/sub", [], True, None),
    # What is above the copy, in the temporary directory, is not looked at,
    # and neither is what the copy leaves out, as at 5241e5f: issue #2.
    "settings-above-the-copy": (
        {"p/.git": "", "above/pytest.ini": _STRICT, "above/copy/.git": ""},
        "p", "above/copy", [], False, None),
    "a-settings-file-the-copy-leaves-out": (
        {"p/pytest.ini": _STRICT, "p/pyproject.toml": "[project]\n",
         "copy/pyproject.toml": "[project]\n"}, "p", "copy", [], False, None),
}


@pytest.mark.parametrize("files, root, where, args, ref, named",
                         list(_SEARCHES.values()), ids=list(_SEARCHES))
def test_pytest_s_search_outside_the_tree_is_refused_only_when_it_matters(
        tmp_path, files, root, where, args, ref, named):
    """pytest's own search, from where a run's paths start it: a file in
    the tree that ends it leaves nothing outside to read; above the
    project's top (the repository's, for a ref), a settings file that sets
    anything but `markers`, or a `conftest.py` pytest loads because its
    rootdir is up there, is refused, naming it."""
    top = os.path.realpath(tmp_path)
    no_pytest_settings_above(top)
    write_tree(top, files)
    root = os.path.join(top, *root.split("/"))
    where = root if where is None else os.path.join(top, *where.split("/"))
    # A copy is the tree unless a ref is said.
    given = {"ref": True} if ref else {}
    if named is None:
        config.check_pytest_settings(root, where, args, **given)
        return
    with pytest.raises(Refusal) as caught:
        config.check_pytest_settings(root, where, args, **given)
    assert str(caught.value).startswith(
        "pytest reads %s, which is above " % os.path.join(
            top, *named.split("/")))


def test_with_no_repository_a_ref_s_tree_is_bounded_at_the_project(
        tmp_path, monkeypatch):
    top = os.path.realpath(tmp_path)
    no_pytest_settings_above(top)
    write_tree(top, {**_REF, "pytest.ini": _STRICT})
    monkeypatch.setattr(config, "_REPOSITORY", (".invective-repository",))
    with pytest.raises(Refusal) as caught:
        config.check_pytest_settings(os.path.join(top, "w", "sub"),
                                     os.path.join(top, "t", "sub"), ref=True)
    assert str(caught.value).startswith(
        "pytest reads %s, which is above the top of the repository %s," % (
            os.path.join(top, "pytest.ini"), os.path.join(top, "w", "sub")))


@pytest.mark.parametrize("name, text", [
    ("pytest.toml", "[pytest]\nxfail_strict = true\n"),
    (".pytest.toml", "[pytest]\nxfail_strict = true\n"),
    ("pytest.ini", "[pytest]\nxfail_strict = true\n"),
    (".pytest.ini", "[pytest]\nxfail_strict = true\n"),
    ("pyproject.toml", "[tool.pytest]\nxfail_strict = true\n"),
    ("tox.ini", "[pytest]\nxfail_strict = true\n"),
    ("setup.cfg", "[tool:pytest]\nxfail_strict = true\n"),
    ("pyproject.toml", "[tool.pytest.ini_options\n"),
])
def test_each_file_pytest_reads_settings_from_is_one(tmp_path, name, text):
    """A file pytest cannot read stops it too, so it is named."""
    top = os.path.realpath(tmp_path)
    write_tree(top, {name: text, "m/.git": ""})
    with pytest.raises(Refusal) as caught:
        config.check_pytest_settings(os.path.join(top, "m"),
                                     os.path.join(top, "m"))
    assert str(caught.value).startswith(
        "pytest reads %s," % os.path.join(top, name))


@pytest.mark.parametrize("name, text", [
    ("tox.ini", "[tox]\nenvlist = py\n"),
    ("setup.cfg", "[metadata]\nname = m\n"),
    ("pyproject.toml", "[tool]\n"),
])
def test_a_file_that_holds_no_pytest_section_is_not_pytest_s(tmp_path, name,
                                                             text):
    """pytest's search goes past it, to the settings above."""
    top = os.path.realpath(tmp_path)
    no_pytest_settings_above(top)
    write_tree(top, {"pytest.ini": _STRICT, "a/" + name: text, "a/m/.git": ""})
    with pytest.raises(Refusal) as caught:
        config.check_pytest_settings(os.path.join(top, "a", "m"),
                                     os.path.join(top, "a", "m"))
    assert str(caught.value).startswith(
        "pytest reads %s," % os.path.join(top, "pytest.ini"))


def test_a_refusal_says_what_to_move_and_offers_no_place_to_run_from(
        tmp_path):
    """A copy made from above the project would carry everything beside it
    into the temporary directory, and a ref's tree is its repository's from
    wherever invective is run: the file is to move into the tree, or the
    project's own settings are to stop pytest before it."""
    top = os.path.realpath(tmp_path)
    no_pytest_settings_above(top)
    root, where = os.path.join(top, "w", "sub"), os.path.join(top, "t", "sub")
    write_tree(top, {**_REF, "pytest.ini": _STRICT})
    said = ("pytest reads %s, which is above %s, so %s the mutants are run in "
            "does not hold it and every run in there would go by other "
            "settings; ")
    for ref, above, held, into in (
            (False, "the project's top " + root, "the copy", "the project"),
            (True, "the top of the repository " + os.path.join(top, "w"),
             "the ref's tree", "the repository")):
        with pytest.raises(Refusal) as caught:
            config.check_pytest_settings(root, where, ref=ref)
        assert str(caught.value) == said % (
            os.path.join(top, "pytest.ini"), above, held) + (
            "move them into %s" % into)

    write_tree(top, {"pytest.ini": "[pytest]\n", "conftest.py": ""})
    for ref, into in ((False, "the project"), (True, "the repository")):
        with pytest.raises(Refusal) as caught:
            config.check_pytest_settings(root, where, ref=ref)
        assert str(caught.value).endswith(
            "; move it into %s, or give the project pytest settings of its "
            "own, which stop pytest there" % into)
        assert str(caught.value).startswith(
            "pytest reads %s," % os.path.join(top, "conftest.py"))


@pytest.mark.parametrize("as_file", [True, False])
def test_a_repository_s_top_is_the_nearest_directory_holding_its_own(
        tmp_path, monkeypatch, as_file):
    """`.git` is a file at a worktree's top; a directory with none on the
    way up is in no repository. The marker is one only this test writes, so
    that a repository the temporary directory is in does not answer."""
    monkeypatch.setattr(config, "_REPOSITORY", (".invective-repository",))
    top = os.path.realpath(tmp_path)
    m = os.path.join(top, "m")
    write_tree(top, {"m/sub/pkg/__init__.py": "", "other/pkg/__init__.py": ""})
    if as_file:
        write_tree(top, {"m/.invective-repository": "gitdir: elsewhere\n"})
    else:
        os.mkdir(os.path.join(m, ".invective-repository"))

    assert config.repository_top(os.path.join(m, "sub", "pkg")) == m
    assert config.repository_top(m) == m
    assert config.repository_top(os.path.join(top, "other", "pkg")) is None


@pytest.mark.parametrize("path, rel", [
    ("pkg/gate.py", os.path.join("pkg", "gate.py")),
    (".", "."),
    # A name that starts with two dots is not the way out.
    ("..notes", "..notes"),
])
def test_a_path_typed_inside_the_project_is_given_from_its_top(tmp_path,
                                                              monkeypatch,
                                                              path, rel):
    top = os.path.realpath(tmp_path)
    monkeypatch.chdir(top)
    assert config.relative_to_root(path, top) == rel


@pytest.mark.parametrize("path", ["..", "../elsewhere"])
def test_a_path_that_leads_out_of_the_project_has_no_place_in_it(tmp_path,
                                                                 monkeypatch,
                                                                 path):
    """Joined to the copy, it would lead out of the copy too."""
    top = os.path.realpath(tmp_path)
    monkeypatch.chdir(top)
    with pytest.raises(ValueError):
        config.relative_to_root(path, top)


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="a symbolic link without privileges")
def test_a_path_typed_through_a_link_to_the_project_is_the_project_s(
        tmp_path):
    """A working directory reached through a link, a logical `$PWD`, gives
    absolute paths through the link while the project's top is the real
    directory: the file they name is the project's own, given from its top,
    and not a file above it."""
    proj = tmp_path / "proj"
    link = tmp_path / "link"
    write_tree(str(proj), {"tests/pytest.ini": "[pytest]\nxfail_strict = 1\n"})
    os.symlink(proj, link)
    top = os.path.realpath(proj)
    typed = str(link / "tests" / "pytest.ini")
    assert config.relative_to_root(typed, top) == os.path.join("tests",
                                                               "pytest.ini")
    # A link inside the project is kept as typed, wherever it leads, and a
    # file outside it is outside.
    write_tree(str(tmp_path), {"elsewhere/pytest.ini": "[pytest]\n"})
    os.symlink(tmp_path / "elsewhere", proj / "out")
    assert config.relative_to_root(str(proj / "out" / "pytest.ini"),
                                   top) == os.path.join("out", "pytest.ini")
    with pytest.raises(ValueError):
        config.relative_to_root(str(tmp_path / "elsewhere" / "pytest.ini"),
                                top)


REPORT = {"survivors": [{}], "accepted": [{}, {}], "stale": [{}]}


@pytest.mark.parametrize("rules, failures", [
    (Config(), []),
    (Config(fail_on_survivors=True),
     ["1 survivor(s) that no comment accepts", "1 stale acceptance(s)"]),
    (Config(max_accepted=2), []),
    (Config(max_accepted=1),
     ["2 accepted survivors, more than max-accepted = 1"]),
])
def test_the_gate_fails_a_run_only_by_the_project_s_rules(rules, failures):
    assert mutate.gate(REPORT, rules) == failures


#: invective's own modules, which accept their survivors in the source.
OWN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), "src")


@pytest.mark.parametrize("module", sorted(
    os.path.relpath(os.path.join(top, name), OWN)
    for top, _dirs, names in os.walk(OWN) for name in names
    if name.endswith(".py")))
def test_every_acceptance_in_invective_s_own_code_names_a_mutant_it_has(
        module):
    """A line wrapped under its acceptance moves the mutant it names to a
    line the acceptance is not about, and the campaign on invective's own
    code says it is stale only when that mutant is among those it runs."""
    with open(os.path.join(OWN, module), encoding="utf-8", newline="") as fh:
        source = fh.read()
    sites = [(node.lineno, what) for _kind, node, what in mutate._sites(
        mutate.ast.parse(source))]
    accepted = accept.read(source, module)
    assert [(a.at, a.change) for a in accepted
            if not any(a.covers(line, what) for line, what in sites)] == []
