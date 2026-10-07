"""Acceptances in the source, the `[tool.invective]` settings, and the gate."""

from __future__ import annotations

import pytest

from invective import accept
from invective import config
from invective import mutate
from invective.config import Config
from invective.errors import Refusal

from conftest import write_tree


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
