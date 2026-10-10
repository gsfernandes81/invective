"""The coverage run's map: which tests ran each line of the target.

Most of these run the selection once as the coverage run, with this
checkout's plugin recording it, and read the map back; the rest read maps
written here, line by line.
"""

from __future__ import annotations

import ast
import json
import os

import pytest

from invective import covers, mutate

from conftest import FILES, SRC, write_tree

GATE_TESTS = "pkg/tests/test_gate.py"
MINOR = "pkg/tests/test_gate.py::test_a_minor_is_refused"
ADULT = "pkg/tests/test_gate.py::test_an_adult_is_admitted"

#: Three tests that run `gate.admit`'s line 6, and one that runs nothing of
#: it, beside the fixture's two.
MORE_TESTS = (
    "\n"
    "def test_one():\n"
    "    assert gate.admit(20, False) == 'adult'\n"
    "\n"
    "def test_none():\n"
    "    pass\n"
    "\n"
    "def test_two():\n"
    "    assert gate.admit(21, False) == 'adult'\n"
    "\n"
    "def test_three():\n"
    "    assert gate.admit(22, False) == 'adult'\n")


def _id(name):
    return "%s::%s" % (GATE_TESTS, name)


def mapped(tmp_path, monkeypatch, files=None, tests=(GATE_TESTS,),
           selection=None, env=None):
    """The map of `pkg/gate.py` that one coverage run of *tests* in the
    fixture tree, with *files* added, records; the plugin is this
    checkout's, as a mutant of it is in a run of invective on itself."""
    root = tmp_path / "tree"
    write_tree(str(root), {**FILES, **(files or {})})
    box = tmp_path / "box"
    box.mkdir()
    monkeypatch.setenv("PYTHONPATH", SRC)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    got = mutate.run_tests(str(root), list(tests), 120, selection, (),
                           "pkg/gate.py", coverage=str(box))
    assert got.ok, got.tail
    return covers.read(str(box), str(root / "pkg" / "gate.py"))


def test_a_line_is_mapped_to_every_test_that_ran_it(tmp_path, monkeypatch):
    """Three tests run line 6, and each is named for it: `sys.monitoring`
    stops a line's event once coverage has seen it, and only its restart at
    each test keeps the second and third."""
    found = mapped(tmp_path, monkeypatch, {
        "pkg/tests/test_gate.py": FILES["pkg/tests/test_gate.py"] + MORE_TESTS})
    assert found.tests == (MINOR, ADULT, _id("test_one"), _id("test_none"),
                           _id("test_two"), _id("test_three"))
    assert found.lines[6] == {"1", "2", "4", "5"}
    assert found.lines[3] == {"0"}
    assert found.lines[2] == {"0", "1", "2", "4", "5"}
    # The top level ran as the module was imported, in no test.
    assert found.lines[1] == {""}


#: A function body's lines that run *what* in a child interpreter started
#: in the tree's top, where `from pkg import gate` finds the package.
_CHILD = ("    import subprocess, sys\n"
          "    subprocess.run([sys.executable, '-c', 'from pkg import gate; "
          "%s'], check=True)\n")


def test_a_line_only_a_child_runs_is_its_test_s(tmp_path, monkeypatch):
    """A child interpreter a test starts is measured too, and its lines are
    its test's; one started once every test has ended is no test's."""
    found = mapped(tmp_path, monkeypatch, {
        "pkg/tests/test_gate.py": FILES["pkg/tests/test_gate.py"]
        + "\ndef test_child():\n" + _CHILD % "gate.admit(70, True)",
        "pkg/tests/conftest.py": FILES["pkg/tests/conftest.py"]
        + "\ndef pytest_sessionfinish():\n" + _CHILD % "gate.admit(66, True)"})
    assert found.lines[5] == {"2", ""}
    assert found.lines[4] == {"1", "2", ""}


def test_the_projects_coverage_settings_are_not_read(tmp_path, monkeypatch):
    """A project's own `omit` would leave the target out of the map, and
    every mutant would run the whole selection."""
    found = mapped(tmp_path, monkeypatch, {
        ".coveragerc": "[run]\nomit = pkg/*\n",
        "pyproject.toml": FILES["pyproject.toml"]
        + "\n[tool.coverage.run]\nomit = ['pkg/*']\n"})
    assert found.lines[3] == {"0"}


def test_a_project_that_makes_warnings_errors_still_gets_a_map(
        tmp_path, monkeypatch):
    """Coverage warns where `sys.monitoring` and contexts meet, and a
    warning made an error would end the coverage run red."""
    found = mapped(tmp_path, monkeypatch, {
        "pyproject.toml": ("[tool.pytest.ini_options]\n"
                           "filterwarnings = ['error']\n")},
        env={"PYTHONWARNINGS": "error"})
    assert found.lines[3] == {"0"}


def test_the_users_coverage_variables_do_not_reach_the_map(
        tmp_path, monkeypatch):
    """A child reads its data file from `COVERAGE_FILE` before the settings
    the coverage run gives it, so the user's would send the child's lines
    where no map is read from."""
    found = mapped(tmp_path, monkeypatch, {
        "pkg/tests/test_gate.py": FILES["pkg/tests/test_gate.py"]
        + "\ndef test_child():\n" + _CHILD % "gate.admit(70, True)"},
        env={"COVERAGE_FILE": str(tmp_path / "elsewhere"),
             "COVERAGE_PROCESS_START": str(tmp_path / "nowhere.ini")})
    assert found.lines[5] == {"2"}


def test_a_selection_file_s_tests_are_the_map_s(tmp_path, monkeypatch):
    """On the plugin's path the run is given a file of node ids, and the map
    names those tests, in the order they ran, and no other."""
    listed = tmp_path / "selection.txt"
    listed.write_text(_id("test_two") + "\n" + ADULT + "\n", encoding="utf-8")
    found = mapped(tmp_path, monkeypatch, {
        "pkg/tests/test_gate.py": FILES["pkg/tests/test_gate.py"] + MORE_TESTS},
        selection=str(listed))
    assert found.tests == (ADULT, _id("test_two"))
    assert found.lines[6] == {"0", "1"}
    assert 3 not in found.lines


# --------------------------------------------------------------------------
# Reading a map


def write_map(where, target, lines, tests=(MINOR, ADULT), name="cov.a"):
    """A coverage run's directory *where*, recording each of *lines*, a line
    to the contexts it ran under, as the plugin would for *target*."""
    from coverage import CoverageData

    os.makedirs(where, exist_ok=True)
    with open(os.path.join(where, "tests.json"), "w", encoding="utf-8") as fh:
        json.dump(list(tests), fh)
    data = CoverageData(basename=os.path.join(where, name))
    by_context = {}
    for line, contexts in lines.items():
        for context in contexts:
            by_context.setdefault(context, []).append(line)
    for context, numbers in by_context.items():
        data.set_context(context)
        data.add_lines({target: numbers})
    data.write()


def test_a_map_is_every_data_file_s_lines_of_the_target_alone(tmp_path):
    where, target = str(tmp_path / "box"), str(tmp_path / "gate.py")
    write_map(where, target, {2: ["0"], 3: ["0"]})
    write_map(where, target, {3: ["1"], 4: [""]}, name="cov.b")
    write_map(where, str(tmp_path / "other.py"), {5: ["1"]}, name="cov.c")
    # Not a data file of the run's, whatever it holds.
    write_map(where, target, {6: ["1"]}, name="covx")
    found = covers.read(where, target)
    assert found == covers.Map((MINOR, ADULT), {2: {"0"}, 3: {"0", "1"},
                                                4: {""}})


@pytest.mark.parametrize("tests", [{"a": 1}, [MINOR, 1], MINOR])
def test_a_list_of_tests_that_is_not_one_is_no_map(tmp_path, tests):
    where = tmp_path / "box"
    write_map(str(where), str(tmp_path / "gate.py"), {2: ["0"]})
    (where / "tests.json").write_text(json.dumps(tests), encoding="utf-8")
    with pytest.raises(ValueError):
        covers.read(str(where), str(tmp_path / "gate.py"))


def test_a_run_that_recorded_no_tests_is_no_map(tmp_path):
    with pytest.raises(OSError):
        covers.read(str(tmp_path), str(tmp_path / "gate.py"))


# --------------------------------------------------------------------------
# A narrower selection

SIX = tuple("t.py::test_%d" % n for n in range(6))

#: A comparison over lines 2 and 3, line 4 another statement's.
SPAN = ast.parse("x = 1\nif (a\n        < b):\n    pass\n").body[1].test


@pytest.mark.parametrize("lines, picked", [
    # Every test that ran a line of the span, in the selection's order.
    ({2: {"2"}, 3: {"0", "2"}}, ("t.py::test_0", "t.py::test_2")),
    # Exactly half the selection is narrower still.
    ({2: {"5", "1", "3"}}, ("t.py::test_1", "t.py::test_3", "t.py::test_5")),
    # A line past the span is another node's.
    ({2: {"1"}, 4: {"0"}}, ("t.py::test_1",)),
    # No line of the span ran.
    ({4: {"0"}}, None),
    # One ran outside any test, as the module was imported.
    ({2: {"1"}, 3: {""}}, None),
    # More than half the selection.
    ({2: {"0", "1"}, 3: {"2", "3"}}, None),
    # A context that is no test's place.
    ({2: {"1", "6"}}, None),
    ({2: {"1", "x"}}, None),
])
def test_the_tests_that_ran_a_node_s_lines_are_its_narrower_selection(
        lines, picked):
    assert (SPAN.lineno, SPAN.end_lineno) == (2, 3)
    assert covers.narrowed(covers.Map(SIX, lines), SPAN) == picked


@pytest.mark.parametrize("size, picked", [(5, 2), (4, 2), (5, 3)])
def test_a_narrower_selection_is_at_most_half_the_selection(size, picked):
    tests = tuple("t.py::test_%d" % n for n in range(size))
    node = ast.parse("x = 1\n").body[0]
    found = covers.narrowed(covers.Map(tests, {1: {str(n) for n in range(
        picked)}}), node)
    assert (found is None) == (2 * picked > size)
