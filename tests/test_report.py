"""The console report and the JSON file, written from a run's findings."""

from __future__ import annotations

import json
import pathlib

from invective import engine, report
from invective.instrument import Mutant
from invective.mutants import Verdict


def _verdict(
    name: str, verdict: str, killer: str | None = None, tests: int = 0, note: str = ""
) -> Verdict:
    """Return *verdict* for the one mutant of the function *name*, in `/here/src`."""
    mutant = Mutant(
        id=f"mod:{name}:body:0",
        ordinal=1,
        module="mod",
        path="/here/src/mod.py",
        qualname=name,
        line=3,
        span=(3, 4),
        description=f"replace {name} -> int with 0",
    )
    return Verdict(mutant, verdict, killer, "call" if killer else None, tests, note)


FOUND = engine.Report(
    targets=["/here/src/mod.py"],
    skipped=["/here/src/invective/finder.py"],
    verdicts=[
        _verdict("a", "killed", killer="tests/test_mod.py::test_a", tests=2),
        _verdict("b", "survived", tests=1),
        _verdict("c", "unreached", note="no test ran it"),
    ],
    baseline={},
    canary="tests/test_mod.py::test_a",
    seconds=1.5,
)


def test_the_console_report_has_a_line_per_mutant_and_per_file_left_out():
    """Each mutant and each file left out has its line, then the canary and the counts."""
    assert report.lines(FOUND, "/here") == [
        "src/mod.py:3  replace a -> int with 0  "
        "killed by tests/test_mod.py::test_a (call)",
        "src/mod.py:3  replace b -> int with 0  SURVIVED 1 test",
        "src/mod.py:3  replace c -> int with 0  UNREACHED: no test ran it",
        f"src/invective/finder.py  skipped: {report.SKIPPED}",
        "canary killed by tests/test_mod.py::test_a",
        "3 mutants in 1 files: 1 killed, 1 survived, 1 unreached, 0 unmeasured; "
        "score 33.3%, 1 killed of 3; 1.5 s",
    ]


def test_the_json_file_holds_every_mutant_and_every_file_left_out(
    tmp_path: pathlib.Path,
) -> None:
    """The JSON file has each mutant with its verdict, and each file left out with why."""
    out = tmp_path / "report.json"
    report.write_json(FOUND, str(out), "/here")
    data = json.loads(out.read_text())
    assert data["targets"] == ["src/mod.py"]
    assert data["skipped"] == [
        {"file": "src/invective/finder.py", "reason": report.SKIPPED}
    ]
    assert data["mutants"][0] == {
        "id": "mod:a:body:0",
        "file": "src/mod.py",
        "line": 3,
        "function": "a",
        "description": "replace a -> int with 0",
        "verdict": "killed",
        "killer": "tests/test_mod.py::test_a",
        "when": "call",
        "tests": 2,
        "note": "",
    }
    assert [m["verdict"] for m in data["mutants"]] == ["killed", "survived", "unreached"]
    assert (data["canary"], data["seconds"]) == ("tests/test_mod.py::test_a", 1.5)
