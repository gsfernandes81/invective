"""The verdict harness's own parts: its rules, its matching, its edits and its recorder.

`tests/test_verdicts.py` runs the harness over the corpus, where a fault in the
harness can pass unseen: a rule that never fires agrees with every engine. These
tests hold each part to its contract on inputs the corpus does not reach.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

import pytest
from harness import arms, check, compare, edit, recorder
from harness.arms import Ran, Verdict
from harness.compare import Entry, Expected, Mutant, Row

from invective import finder

KILLED = Verdict("killed", "t::test")
SURVIVED = Verdict("survived")


@pytest.mark.parametrize(
    ("found", "outside", "fired"),
    [
        pytest.param(
            {"engine": KILLED, "truth": SURVIVED},
            False,
            ["1"],
            id="kill-against-survivor",
        ),
        pytest.param(
            {"engine": Verdict("unreached"), "truth": KILLED},
            False,
            ["2"],
            id="false-unreached",
        ),
        pytest.param(
            {"engine": Verdict("unmeasured", note="no reason"), "truth": KILLED},
            False,
            ["3"],
            id="unmeasured-unexplained",
        ),
        pytest.param(
            {"engine": Verdict("unmeasured", note="ran at import"), "truth": KILLED},
            True,
            [],
            id="unmeasured-at-import-seen-outside",
        ),
        pytest.param(
            {"engine": Verdict("unmeasured", note="ran at import"), "truth": SURVIVED},
            False,
            ["3"],
            id="unmeasured-at-import-never-outside",
        ),
        pytest.param(
            {
                "engine": Verdict("unmeasured", note="over its wall budget"),
                "truth": KILLED,
            },
            False,
            [],
            id="unmeasured-over-budget",
        ),
        pytest.param(
            {
                "engine": Verdict(
                    "unmeasured", note="the child exited 5 without a verdict"
                ),
                "truth": Verdict("killed", "t::test", plain=False),
            },
            False,
            [],
            id="unmeasured-exit-against-exit",
        ),
        pytest.param(
            {
                "engine": Verdict("unmeasured", note="ran at import"),
                "truth": Verdict("unreached"),
            },
            True,
            ["6"],
            id="unmeasured-against-unreached",
        ),
        pytest.param(
            {"engine": Verdict("unmeasured"), "truth": Verdict("timeout")},
            False,
            ["6"],
            id="unmeasured-against-timeout",
        ),
        pytest.param(
            {"engine": KILLED, "truth": Verdict("unreached")},
            False,
            ["6"],
            id="kill-against-unreached",
        ),
        pytest.param(
            {"engine": SURVIVED, "subprocess": Verdict("timeout")},
            False,
            ["5"],
            id="engine-against-subprocess",
        ),
        pytest.param(
            {"engine": Verdict("unreached"), "subprocess": SURVIVED},
            False,
            [],
            id="unreached-against-subprocess-survivor",
        ),
        pytest.param(
            {"engine": Verdict("unmeasured"), "subprocess": KILLED},
            False,
            [],
            id="unmeasured-left-to-rule-3",
        ),
        pytest.param(
            {"engine": KILLED, "subprocess": KILLED, "truth": KILLED},
            False,
            [],
            id="agree",
        ),
    ],
)
def test_the_rules_that_fire(
    found: dict[str, Verdict], outside: bool, fired: list[str]
) -> None:
    """Each rule fires on the verdicts it covers, and on no others."""
    assert [rule.partition(":")[0] for rule in compare.rules(found, outside)] == fired


MUTANT = Mutant("src/p/m.py", 1, "f", "0")
OTHER = Mutant("src/p/m.py", 2, "f", "1")


def _entry(**given: Any) -> Entry:
    """Return an entry for `MUTANT` that expects arm 1 to kill and arm 3 to survive."""
    entry = Entry(
        MUTANT.path,
        MUTANT.ordinal,
        None,
        None,
        1,
        {"engine": "killed", "truth": "survived"},
        "a reason",
    )
    return entry._replace(**given)


def _expected(*entries: Entry, project: str | None = None) -> Expected:
    """Return an expected-disagreement file holding *entries*."""
    return Expected(project, list(entries), None, [], [])


DISAGREES = Row(MUTANT, 1, {"engine": KILLED, "truth": SURVIVED}, ("1: ...",))
AGREES = Row(OTHER, 1, {"engine": KILLED, "truth": KILLED}, ())


@pytest.mark.parametrize(
    ("rows", "project", "expected", "differences"),
    [
        pytest.param(
            [DISAGREES, AGREES], None, _expected(_entry()), [], id="as-expected"
        ),
        pytest.param([DISAGREES], None, _expected(), ["unexpected:"], id="unexpected"),
        pytest.param(
            [AGREES],
            None,
            _expected(_entry(ordinal=2)),
            ["expected, not seen:"],
            id="not-seen",
        ),
        pytest.param(
            [DISAGREES],
            None,
            _expected(_entry(arms={"engine": "killed", "truth": "unreached"})),
            ["unexpected:", "expected, not seen:"],
            id="other-verdicts",
        ),
        pytest.param(
            [DISAGREES],
            None,
            _expected(_entry(), _entry(ordinal=9)),
            ["expected entry names no mutant:"],
            id="names-nothing",
        ),
        pytest.param(
            [DISAGREES],
            None,
            _expected(_entry(workers=4)),
            ["unexpected:", "expected, not seen:"],
            id="other-workers",
        ),
        pytest.param([], "refused", _expected(), ["project:"], id="project-unexpected"),
        pytest.param(
            [], None, _expected(project="crashed"), ["project:"], id="project-not-seen"
        ),
    ],
)
def test_the_differences_from_the_expected_file(
    rows: list[Row], project: str | None, expected: Expected, differences: list[str]
) -> None:
    """The comparison names every difference, in both directions, and nothing else."""
    found = compare.unmatched(rows, project, expected, [MUTANT, OTHER])
    assert [line.partition(":")[0] + ":" for line in found] == differences


# Each shape: the source, the function mutated, and how to call it in the edited
# module's namespace. Every mutant here replaces the function's body with 42.
SHAPES = {
    "global-after-docstring": (
        'X = 1\n\n\ndef f():\n    """Doc."""\n    global X\n    X = 2\n    return X\n',
        "f",
        "f()",
    ),
    "nonlocal": (
        "def outer():\n    n = 0\n\n    def f():\n        nonlocal n\n        n += 1\n"
        "        return n\n\n    return f\n",
        "outer.<locals>.f",
        "outer()()",
    ),
    "decorated-def-first": (
        "def f():\n    @staticmethod\n    def g():\n        return 1\n\n    return g\n",
        "f",
        "f()",
    ),
    "crlf": ("def f():\r\n    y = 1\r\n    return y\r\n", "f", "f()"),
    "lone-cr": ("def f():\r    y = 1\r    return y\r", "f", "f()"),
    "tabs": ("def f():\n\ty = 1\n\treturn y\n", "f", "f()"),
    "non-ascii-block": ('def f():\n    s = "é"\n    return s\n', "f", "f()"),
    "non-ascii-inline": ("def f(é=1): return é\n", "f", "f()"),
    "one-line-generator": ("def f(): yield 1\n", "f", "list(f())"),
    "async-generator": (
        "async def f():\n    yield 1\n",
        "f",
        "asyncio.run(collect(f()))",
    ),
    "trailing-comment": ("def f():\n    return 1  # one\n", "f", "f()"),
    "continuation": ("def f():\n    return 1 + \\\n        2\n", "f", "f()"),
}


async def _collect(generator):
    """Return what an async generator yields, as a list."""
    return [item async for item in generator]


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_mutant_edit_replaces_the_function_body(
    shape: str, tmp_path: pathlib.Path
) -> None:
    """The edit gives every mutant of the table a site, and applies one as its value.

    The site's key is the instrumenter's, so a site found at another line (a
    declaration not skipped, a decorator miscounted) leaves the mutant without one.
    """
    text, function, call = SHAPES[shape]
    path = tmp_path / "m.py"
    path.write_bytes(text.encode())
    source = path.read_bytes()
    found = edit.sites(source)
    mutants = finder.table(str(path)).mutants
    assert mutants
    for mutant in mutants:
        assert (mutant.qualname, *mutant.span) in found, (mutant, sorted(found))

    (site,) = [site for key, site in found.items() if key[0] == function]
    value = "iter([42])" if site.generator else "42"
    namespace: dict[str, object] = {"asyncio": asyncio, "collect": _collect}
    exec(compile(edit.mutate(source, site, value), str(path), "exec"), namespace)
    assert eval(call, namespace) == ([42] if site.generator else 42)


def test_a_function_whose_body_is_only_a_docstring_has_no_site() -> None:
    """Such a function has no mutant, so the edit gives it no site and does not fail."""
    found = edit.sites(b'def f():\n    """Doc."""\n\n\ndef g():\n    return 1\n')
    assert [key[0] for key in found] == ["g"]


def _project(tmp_path: pathlib.Path, files: dict[str, str]) -> str:
    """Write a project of *files* under *tmp_path*, and return its path."""
    root = tmp_path / "project"
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return str(root)


def _record(
    tmp_path: pathlib.Path, files: dict[str, str], **extra: str
) -> tuple[Ran, list[dict]]:
    """Run plain pytest with the recorder on a project of *files*; return the run."""
    root = _project(tmp_path, files)
    env = arms.environ(arms.install_recorder(str(tmp_path / "plugins")), **extra)
    results = str(tmp_path / "results.jsonl")
    ran = arms.pytest(
        root, ["-p", recorder.MODULE, "-p", "no:invective"], env, 60, results
    )
    return ran, ran.lines


def test_the_recorder_writes_a_failed_collection(tmp_path: pathlib.Path) -> None:
    """A module that fails to import is written as a failed collection, which kills."""
    ran, lines = _record(
        tmp_path,
        {
            "test_ok.py": "def test_ok():\n    pass\n",
            "test_bad.py": "import nowhere_to_be_found\n",
        },
    )
    assert {"event": "collect", "nodeid": "test_bad.py"} in lines
    assert arms.judge(ran, 60).verdict == "killed"


def test_the_recorder_keeps_the_selected_tests_in_their_order(
    tmp_path: pathlib.Path,
) -> None:
    """With a selection file, the run keeps the tests it names, in its order."""
    select = tmp_path / "select.txt"
    select.write_text("test_t.py::test_c\ntest_t.py::test_a\n")
    _, lines = _record(
        tmp_path,
        {"test_t.py": "".join(f"def test_{n}():\n    pass\n\n\n" for n in "abc")},
        **{recorder.SELECT: str(select)},
    )
    started = [line["nodeid"] for line in lines if line["event"] == "start"]
    assert started == ["test_t.py::test_c", "test_t.py::test_a"]


def test_a_failure_of_a_test_the_baseline_never_ran_kills() -> None:
    """A parameter a mutant adds, failing, kills it, though the baseline never ran it."""
    lines = [
        {"event": "start", "nodeid": "t.py::test[0]"},
        {
            "event": "report",
            "nodeid": "t.py::test[0]",
            "when": "call",
            "outcome": "failed",
        },
        {"event": "finish", "exitstatus": 1},
    ]
    verdict = arms.judge(Ran(1, "", 0.1, False, lines), 60)
    assert verdict == Verdict("killed", "t.py::test[0]", "call")


@pytest.mark.parametrize(
    ("suffix", "truth"),
    [(edit.IN_TEST, "survived"), (edit.OUTSIDE, "survived"), (None, "unreached")],
)
def test_a_survivor_whose_function_never_ran_is_unreached(
    suffix: str | None, truth: str
) -> None:
    """Arm 3's survivor is unreached only when its function never ran in the probe run.

    A run outside any test, at import for one, counts as running.
    """
    seen = {edit.probe_file("p", suffix)} if suffix else set()
    row = check._row(MUTANT, 1, None, (SURVIVED, SURVIVED, "p"), seen)
    assert row.arms["truth"].verdict == truth


def test_the_harness_reads_python_files_from_every_settings_file(
    tmp_path: pathlib.Path,
) -> None:
    """The test module patterns come from the first file pytest itself would read."""
    for name, text in [
        ("pytest.ini", "[pytest]\npython_files = check_*.py\n"),
        ("tox.ini", "[pytest]\npython_files = check_*.py\n"),
        ("setup.cfg", "[tool:pytest]\npython_files = check_*.py\n"),
        ("pyproject.toml", '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n'),
    ]:
        root = tmp_path / name
        root.mkdir()
        (root / name).write_text(text)
        assert check._python_files(str(root)) == ["check_*.py"], name
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "pytest.ini").write_text("")
    (empty / "tox.ini").write_text("[pytest]\npython_files = check_*.py\n")
    assert check._python_files(str(empty)) == ["test_*.py", "*_test.py"]
