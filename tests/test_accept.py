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


def test_pytest_settings_above_the_project_s_top_are_named(tmp_path):
    """The workspace's settings, which pytest reads from inside the member
    and a copy of the member does not hold; none when the member has its
    own, or when the file above sets nothing for pytest."""
    top = os.path.realpath(tmp_path)
    no_pytest_settings_above(top)
    member = os.path.join(top, "m")
    write_tree(top, {"pyproject.toml":
                     "[tool.pytest.ini_options]\nxfail_strict = true\n",
                     "m/pyproject.toml": ""})
    assert config.pytest_config_above(member) == os.path.join(
        top, "pyproject.toml")

    write_tree(top, {"m/pytest.ini": ""})
    assert config.pytest_config_above(member) is None

    os.remove(os.path.join(member, "pytest.ini"))
    write_tree(top, {"pyproject.toml": "[tool.ruff]\nline-length = 79\n"})
    assert config.pytest_config_above(member) is None


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
    assert config.pytest_config_above(os.path.join(top, "m")) == os.path.join(
        top, name)


@pytest.mark.parametrize("name, text", [
    ("tox.ini", "[tox]\nenvlist = py\n"),
    ("setup.cfg", "[metadata]\nname = m\n"),
    ("pyproject.toml", "[tool]\n"),
])
def test_a_file_that_holds_no_pytest_section_is_not_pytest_s(tmp_path, name,
                                                             text):
    top = os.path.realpath(tmp_path)
    no_pytest_settings_above(top)
    write_tree(top, {name: text, "m/.git": ""})
    assert config.pytest_config_above(os.path.join(top, "m")) is None


def test_an_empty_pytest_table_does_not_stop_pytest_s_search(tmp_path):
    """`[tool.pytest]` with nothing in it is no settings file to pytest,
    which goes on up to the one that is."""
    top = os.path.realpath(tmp_path)
    write_tree(top, {"pytest.ini": "[pytest]\nxfail_strict = true\n",
                     "a/pyproject.toml": "[tool.pytest]\n", "a/m/.git": ""})
    assert config.pytest_config_above(os.path.join(top, "a", "m")) == (
        os.path.join(top, "pytest.ini"))


def test_the_search_starts_where_pytest_s_does_among_the_tests(tmp_path):
    """pytest looks from the deepest directory every test path is under: the
    tests' own `pytest.ini` comes before the workspace's, and a node id or
    an option among them changes nothing."""
    top = os.path.realpath(tmp_path)
    member = os.path.join(top, "m")
    write_tree(top, {"pytest.ini": "[pytest]\nxfail_strict = true\n",
                     "m/.git": "", "m/tests/pytest.ini": "",
                     "m/tests/test_a.py": "", "m/tests/sub/test_b.py": ""})
    assert config.pytest_config_above(
        member, ["tests/test_a.py::test_x", "-q", "tests/sub"]) is None
    assert config.pytest_config_above(member) == os.path.join(top,
                                                              "pytest.ini")
    # Each kind of path alone starts the search at the tests' directory.
    assert config.pytest_config_above(member, ["tests/test_a.py"]) is None
    assert config.pytest_config_above(member, ["tests/sub"]) is None
    # A path that names nothing, or leads out, leaves the search at the top.
    assert config.pytest_config_above(member, ["tests/none.py", ".."]) == (
        os.path.join(top, "pytest.ini"))


@pytest.mark.parametrize("files, args, given", [
    ({"tests/pytest.ini": "[pytest]\n", "pyproject.toml": "[project]\n"},
     ["tests"], ("-c", os.path.join("tests", "pytest.ini"))),
    ({"pyproject.toml": "[project]\n"}, [], ("-c", "pyproject.toml")),
    ({".git": ""}, [], ()),
], ids=["below-the-top", "pyproject-fallback", "none"])
def test_every_run_is_given_the_settings_file_pytest_reads_here(
        tmp_path, files, args, given):
    """The file pytest reads for the run started here, given from the top,
    whether it sets something or is only where pytest's search fell back."""
    no_pytest_settings_above(tmp_path)
    proj = os.path.join(os.path.realpath(tmp_path), "proj")
    write_tree(proj, files)
    assert config.pytest_settings(proj, proj, args) == given


def test_the_settings_file_given_to_every_run_is_searched_no_further_than_the_top(
        tmp_path):
    """Run in a copy of the top, pytest's search would go on above the
    temporary directory: what the top's own pytest falls back on is given,
    and a file above it that sets nothing is let be."""
    top = os.path.realpath(tmp_path)
    write_tree(top, {"pytest.ini": "[pytest]\n",
                     "m/pyproject.toml": "[project]\n"})
    m = os.path.join(top, "m")
    assert config.pytest_settings(m, m) == ("-c", "pyproject.toml")


def test_a_settings_file_above_the_top_is_given_to_no_run(tmp_path):
    """The copy has no path to it, and one that sets nothing is no reason
    to refuse."""
    top = os.path.realpath(tmp_path)
    write_tree(top, {"pytest.ini": "[pytest]\n", "m/.git": ""})
    m = os.path.join(top, "m")
    assert config.pytest_settings(m, m) == ()


def test_a_settings_file_above_the_project_inside_its_top_is_given_with_dots(
        tmp_path):
    """For a ref the search goes as far as its repository's top, and a file
    found above the project there is given from the project with `..`; for
    a copy it ends at the project, whose `pyproject.toml` pytest falls back
    on. Above the repository, a file that sets nothing is let be."""
    top = os.path.realpath(tmp_path)
    write_tree(top, {"pytest.ini": "[pytest]\n", "m/.git": "",
                     "m/sub/pyproject.toml": "[project]\n"})
    sub = os.path.join(top, "m", "sub")

    assert config.pytest_settings(sub, sub) == ("-c", "pyproject.toml")
    assert config.pytest_settings(sub, sub, ref=True) == (
        "-c", "pyproject.toml")
    os.rename(os.path.join(top, "pytest.ini"),
              os.path.join(top, "m", "pytest.ini"))
    assert config.pytest_settings(sub, sub, ref=True) == (
        "-c", os.path.join("..", "pytest.ini"))
    assert config.pytest_settings(sub, sub) == ("-c", "pyproject.toml")


#: A repository `w` and the tree of one of its refs `t`, each with the
#: project `sub` in it, whose own `pyproject.toml` sets nothing. A `.git`
#: file marks each one's top, as it does a worktree's.
_REF = {"w/.git": "gitdir: elsewhere\n", "w/sub/pyproject.toml": "[project]\n",
        "t/.git": "gitdir: elsewhere\n", "t/sub/pyproject.toml": "[project]\n"}
_STRICT = "[pytest]\nxfail_strict = true\n"


def test_a_ref_s_runs_go_by_the_settings_in_its_own_tree(tmp_path):
    """The repository's settings, above the project, are refused for a copy
    of the project, which does not hold them; a ref's tree holds its own,
    which every run there is given. What the files as they stand keep
    there is not the ref's: with none in the ref's tree, its runs fall back
    on the project's `pyproject.toml`, as the ref's own pytest does."""
    no_pytest_settings_above(tmp_path)
    top = os.path.realpath(tmp_path)
    write_tree(top, {**_REF, "w/pytest.ini": _STRICT, "t/pytest.ini": _STRICT})
    root, where = os.path.join(top, "w", "sub"), os.path.join(top, "t", "sub")

    with pytest.raises(Refusal) as caught:
        config.pytest_settings(root, root)
    assert str(caught.value).startswith(
        "pytest reads %s, which is above the project's top %s, so the copy "
        "the mutants are run in does not hold it" % (
            os.path.join(top, "w", "pytest.ini"), root))
    assert config.pytest_settings(root, where, ref=True) == (
        "-c", os.path.join("..", "pytest.ini"))

    os.remove(os.path.join(top, "t", "pytest.ini"))
    assert config.pytest_settings(root, where, ref=True) == (
        "-c", "pyproject.toml")


def test_settings_above_a_repository_are_in_no_ref_s_tree(tmp_path,
                                                         monkeypatch):
    """A file above the repository's top is in no commit of it: refused for
    a ref, naming it, unless the ref's own settings stop pytest's search
    first. With no repository found, the project's top is the tree's."""
    no_pytest_settings_above(tmp_path)
    top = os.path.realpath(tmp_path)
    write_tree(top, {**_REF, "pytest.ini": _STRICT})
    root, where = os.path.join(top, "w", "sub"), os.path.join(top, "t", "sub")
    above = os.path.join(top, "pytest.ini")

    with pytest.raises(Refusal) as caught:
        config.pytest_settings(root, where, ref=True)
    assert str(caught.value) == (
        "pytest reads %s, which is above the top of the repository %s, so the "
        "ref's tree the mutants are run in does not hold it and every run in "
        "there would go by other settings; move them into the repository"
        % (above, os.path.join(top, "w")))

    write_tree(top, {"t/sub/pyproject.toml": "[tool.pytest.ini_options]\n"})
    assert config.pytest_settings(root, where, ref=True) == (
        "-c", "pyproject.toml")

    monkeypatch.setattr(config, "_REPOSITORY", (".invective-repository",))
    write_tree(top, {"t/sub/pyproject.toml": "[project]\n"})
    with pytest.raises(Refusal) as caught:
        config.pytest_settings(root, where, ref=True)
    assert str(caught.value).startswith("pytest reads %s, which is above "
                                        % above)


def test_a_settings_file_the_copy_leaves_out_is_refused_and_not_replaced(
        tmp_path):
    """The run started here reads the project's `pytest.ini`; a copy that
    leaves it out would have every run go by its `pyproject.toml`."""
    no_pytest_settings_above(tmp_path)
    top = os.path.realpath(tmp_path)
    write_tree(top, {"proj/pytest.ini": _STRICT,
                     "proj/pyproject.toml": "[project]\n",
                     "copy/pyproject.toml": "[project]\n"})
    root, where = os.path.join(top, "proj"), os.path.join(top, "copy")

    with pytest.raises(Refusal) as caught:
        config.pytest_settings(root, where)
    assert str(caught.value) == (
        "pytest read its settings from pytest.ini, which the tree the mutants "
        "are made in does not hold (excluded, ignored by git, or not in the "
        "ref), so no run in there can go by the settings this one did")
    write_tree(top, {"copy/pytest.ini": _STRICT})
    assert config.pytest_settings(root, where) == ("-c", "pytest.ini")


def test_settings_above_the_copy_refuse_a_project_with_none(tmp_path):
    """With no file of the project's to give, every run's search goes on
    above the copy, and settings there would decide it."""
    no_pytest_settings_above(tmp_path)
    top = os.path.realpath(tmp_path)
    write_tree(top, {"proj/.git": "", "above/pytest.ini": _STRICT,
                     "above/copy/.git": ""})
    root, where = os.path.join(top, "proj"), os.path.join(top, "above", "copy")

    with pytest.raises(Refusal) as caught:
        config.pytest_settings(root, where)
    assert str(caught.value).startswith(
        "pytest reads %s, above the copy the mutants are run in at %s, so "
        "every run in there would go by it;" % (
            os.path.join(top, "above", "pytest.ini"), where))
    assert config.pytest_settings(root, root) == ()


def test_the_settings_file_a_pytest_read_is_given_from_the_project(tmp_path):
    """`pytest --mutate` names the file it read, given from the project's
    top to every run. A file outside the project is given to no run: let be
    when it sets nothing, refused when it sets something, or when it was
    named with `-c`, which decides the run whatever it sets."""
    no_pytest_settings_above(tmp_path)
    top = os.path.realpath(tmp_path)
    write_tree(top, {**_REF, "pytest.ini": "[pytest]\n",
                     "w/sub/tests/pytest.ini": "[pytest]\n",
                     "elsewhere/pytest.ini": "[pytest]\n",
                     "strict/pytest.ini": _STRICT})
    root = os.path.join(top, "w", "sub")
    below = os.path.join(root, "tests", "pytest.ini")
    elsewhere = os.path.join(top, "elsewhere", "pytest.ini")
    strict = os.path.join(top, "strict", "pytest.ini")

    for given in (False, True):
        assert config.pytest_settings(root, root, read=below,
                                      given=given) == (
            "-c", os.path.join("tests", "pytest.ini"))
    assert config.pytest_settings(
        root, root, read=os.path.join(top, "pytest.ini")) == ()
    with pytest.raises(Refusal) as caught:
        config.pytest_settings(root, root, read=strict)
    assert str(caught.value).startswith(
        "pytest reads %s, which is above the project's top %s," % (strict,
                                                                   root))
    with pytest.raises(Refusal) as caught:
        config.pytest_settings(root, root, read=elsewhere, given=True)
    assert str(caught.value) == (
        "pytest's settings file %s, given with -c, is outside the project at "
        "%s: no copy holds it, and every mutant's run would read the "
        "project's own settings instead" % (elsewhere, root))


def test_the_settings_file_a_pytest_read_is_given_from_the_project_in_a_ref(
        tmp_path):
    """For a ref, from the project's place in the ref's tree, with `..` for
    the repository's own above the project, and refused when the ref's tree
    does not hold it. Named with `-c` from outside the repository, no ref's
    tree holds it."""
    no_pytest_settings_above(tmp_path)
    top = os.path.realpath(tmp_path)
    write_tree(top, {**_REF, "w/pytest.ini": _STRICT, "t/pytest.ini": _STRICT,
                     "elsewhere/pytest.ini": "[pytest]\n"})
    root, where = os.path.join(top, "w", "sub"), os.path.join(top, "t", "sub")
    repository = os.path.join(top, "w", "pytest.ini")
    elsewhere = os.path.join(top, "elsewhere", "pytest.ini")

    assert config.pytest_settings(root, where, ref=True, read=repository) == (
        "-c", os.path.join("..", "pytest.ini"))
    with pytest.raises(Refusal) as caught:
        config.pytest_settings(root, where, ref=True, read=elsewhere,
                               given=True)
    assert str(caught.value) == (
        "pytest's settings file %s, given with -c, is outside the repository "
        "at %s: no ref's tree holds it, and every mutant's run would read "
        "the ref's own settings instead" % (elsewhere, os.path.join(top, "w")))

    os.remove(os.path.join(top, "t", "pytest.ini"))
    with pytest.raises(Refusal) as caught:
        config.pytest_settings(root, where, ref=True, read=repository)
    assert str(caught.value).startswith(
        "pytest read its settings from %s, which the tree the mutants are "
        "made in does not hold" % os.path.join("..", "pytest.ini"))


def test_a_conftest_above_a_project_that_falls_back_on_its_own_pyproject_is_let_be(
        tmp_path):
    """With no file on the way up that is pytest's, pytest falls back on
    the nearest `pyproject.toml`, the project's own, and its rootdir is
    there: a `conftest.py` above is never loaded. With none in the project,
    the one above is the nearest, and the `conftest.py` below it is."""
    home = os.path.realpath(tmp_path)
    no_pytest_settings_above(home)
    proj = os.path.join(home, "work", "proj")
    write_tree(home, {"pyproject.toml": "[tool.ruff]\n",
                      "work/conftest.py": "",
                      "work/proj/pyproject.toml": "[project]\n"})
    assert config.pytest_config_above(proj) is None

    os.remove(os.path.join(proj, "pyproject.toml"))
    assert config.pytest_config_above(proj) == os.path.join(home, "work",
                                                            "conftest.py")


def test_a_ref_s_refusal_names_its_repository_and_no_place_to_run_from(
        tmp_path, monkeypatch):
    """A ref's tree is its repository's from wherever invective is run, so
    the only way to a tree that holds the file is to move it into the
    repository; for a copy, running from the file's directory is one."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    ws, repo = str(tmp_path / "ws"), str(tmp_path / "ws" / "repo")
    ini = os.path.join(ws, "pytest.ini")

    assert str(config.outside_refusal(ini, repo, ref=True)) == (
        "pytest reads %s, which is above the top of the repository %s, so the "
        "ref's tree the mutants are run in does not hold it and every run in "
        "there would go by other settings; move them into the repository"
        % (ini, repo))
    assert str(config.outside_refusal(
        os.path.join(ws, "conftest.py"), repo, ref=True)).endswith(
        "; move it into the repository, or give the project pytest settings "
        "of its own, which stop pytest there")
    assert str(config.outside_refusal(ini, repo)).endswith(
        "; move them into the project, or run invective from %s, so that "
        "the copy is made from there" % ws)
    assert str(config.outside_refusal(
        os.path.join(ws, "conftest.py"), repo)).endswith(
        "; move it into the project, or give the project pytest settings of "
        "its own, which stop pytest there")


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


def test_the_home_directory_is_not_offered_as_where_to_run_from(
        tmp_path, monkeypatch):
    """Running from the home directory would copy all of it into the temporary
    directory, keys and `.env` files with it; a workspace below it is the
    place to run from."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    home = str(config.outside_refusal(str(tmp_path / "pyproject.toml"),
                                      str(tmp_path / "proj")))
    above = str(config.outside_refusal(
        str(tmp_path.parent / "pyproject.toml"), str(tmp_path / "proj")))
    ws = str(config.outside_refusal(str(tmp_path / "ws" / "pyproject.toml"),
                                    str(tmp_path / "ws" / "proj")))

    top = os.path.join(os.path.abspath(os.sep), "pytest.ini")
    root = str(config.outside_refusal(top, str(tmp_path / "proj")))

    assert "run invective from" not in home
    assert "move them into the project" in home
    assert "run invective from" not in above
    # The filesystem's top is above the home directory like any other.
    assert "run invective from" not in root
    assert "move them into the project" in root
    assert "run invective from %s," % (tmp_path / "ws") in ws


def test_a_root_of_the_filesystem_is_never_offered_as_where_to_run_from(
        tmp_path, monkeypatch):
    """A drive's root need not hold the home directory (`D:\\` while it is on
    `C:`), and a copy made from it is a copy of the whole drive. Here every
    root holds the home directory, so the home rule is set aside to see the
    root rule alone."""
    monkeypatch.setattr(config, "_inside", lambda path, root: None)

    top = os.path.join(os.path.abspath(os.sep), "pytest.ini")
    root = str(config.outside_refusal(top, str(tmp_path / "proj")))
    below = str(config.outside_refusal(str(tmp_path / "ws" / "pytest.ini"),
                                       str(tmp_path / "ws" / "proj")))

    assert "run invective from" not in root
    assert "move them into the project" in root
    assert "run invective from %s," % (tmp_path / "ws") in below


def test_a_conftest_pytest_loads_above_the_project_s_top_is_named(tmp_path):
    """Settings that set nothing still put pytest's rootdir above the top,
    and pytest loads every `conftest.py` from there down, which the copy
    does not hold. With none, a `pyproject.toml` of a tool's settings in the
    home directory is no reason to refuse."""
    home = os.path.realpath(tmp_path)
    no_pytest_settings_above(home)
    proj = os.path.join(home, "work", "proj")
    write_tree(home, {"pyproject.toml": "[tool.ruff]\n",
                      "work/proj/.git": ""})
    assert config.pytest_config_above(proj) is None

    write_tree(home, {"work/conftest.py": ""})
    assert config.pytest_config_above(proj) == os.path.join(home, "work",
                                                            "conftest.py")
    # The plugin asks with the file its pytest found.
    assert config.pytest_file_left_out(
        proj, os.path.join(home, "pyproject.toml")) == os.path.join(
            home, "work", "conftest.py")
    # From the settings file's directory down: one at the home directory
    # too, and not one above the nearest `pyproject.toml`, where pytest's
    # rootdir is when none of them is pytest's.
    os.remove(os.path.join(home, "work", "conftest.py"))
    write_tree(home, {"conftest.py": ""})
    assert config.pytest_config_above(proj) == os.path.join(home,
                                                            "conftest.py")
    write_tree(home, {"work/pyproject.toml": ""})
    assert config.pytest_config_above(proj) is None
    assert "give the project pytest settings of its own" in str(
        config.outside_refusal(os.path.join(home, "work", "conftest.py"),
                               proj))
    assert config.pytest_file_left_out(proj, None) is None
    # A settings file beside the project (`-c`) loads no `conftest.py` above
    # it: pytest loads them from the settings file's directory down.
    write_tree(home, {"elsewhere/pytest.ini": ""})
    assert config.pytest_file_left_out(
        proj, os.path.join(home, "elsewhere", "pytest.ini")) is None
    write_tree(home, {"work/proj/pytest.ini": "[pytest]\nxfail_strict = 1\n"})
    assert config.pytest_file_left_out(
        proj, os.path.join(proj, "pytest.ini")) is None


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
    assert config.pytest_file_left_out(top, typed) is None
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
