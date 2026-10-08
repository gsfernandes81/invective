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
def test_a_repository_s_directory_is_the_top_when_no_pyproject_comes_first(
        tmp_path, name, as_file):
    """A `pyproject.toml` of a tool's settings in the home directory must not
    make the home directory the project. `.git` is a file in a worktree."""
    home = os.path.realpath(tmp_path)
    proj = os.path.join(home, "proj")
    write_tree(home, {"pyproject.toml": "", "proj/pkg/__init__.py": ""})
    if as_file:
        write_tree(home, {"proj/" + name: "gitdir: elsewhere\n"})
    else:
        os.mkdir(os.path.join(proj, name))
    assert config.project_root(os.path.join(proj, "pkg")) == proj

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
    directory is not copied."""
    home = os.path.join(os.path.realpath(tmp_path), "home")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    proj = os.path.join(home, "proj")
    write_tree(home, {marker: "[tool.ruff]\n", "proj/gate.py": ""})
    assert config.project_root(proj) == proj
    top = home if marker != "../.git" else os.path.dirname(home)
    assert config.project_root(home) == top

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
    assert config.pytest_settings_option(proj, args) == given


def test_a_settings_file_above_the_top_is_given_to_no_run(tmp_path):
    """The copy has no path to it: whether the runs may go without it is
    `pytest_config_above`'s to say."""
    top = os.path.realpath(tmp_path)
    write_tree(top, {"pytest.ini": "[pytest]\n", "m/.git": ""})
    assert config.pytest_settings_option(os.path.join(top, "m")) == ()


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
