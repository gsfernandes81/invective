"""The mutation report of a run: the console lines and the JSON file."""

from __future__ import annotations

import json
import os

from invective.engine import Report
from invective.mutants import Verdict

#: The verdicts, in the order the summary counts them.
VERDICTS = ("killed", "survived", "unreached", "unmeasured")

#: Why a file named for mutation was left out of the run.
SKIPPED = (
    "part of the harness that serves instrumented code, imported before the finder exists"
)


def lines(report: Report, here: str) -> list[str]:
    """Return the console report: a line per mutant and per file left out, then totals.

    The totals are the canary's killer and the counts. Paths are relative to *here*.
    The score counts an unreached mutant as a survivor and leaves an unmeasured one
    out.
    """
    width = max((len(_where(v, here)) for v in report.verdicts), default=0)
    out = [
        f"{_where(verdict, here):<{width}}  {verdict.mutant.description}  "
        f"{_said(verdict)}"
        for verdict in report.verdicts
    ]
    out += [
        f"{os.path.relpath(path, here)}  skipped: {SKIPPED}" for path in report.skipped
    ]

    counts = {
        name: sum(1 for v in report.verdicts if v.verdict == name) for name in VERDICTS
    }
    scored = counts["killed"] + counts["survived"] + counts["unreached"]
    score = (
        f"{100.0 * counts['killed'] / scored:.1f}%, {counts['killed']} killed of {scored}"
        if scored
        else "none, nothing to score"
    )
    tally = ", ".join(f"{counts[name]} {name}" for name in VERDICTS)
    out.append(f"canary killed by {report.canary}")
    out.append(
        f"{len(report.verdicts)} mutants in {len(report.targets)} files: {tally}; "
        f"score {score}; {report.seconds:.1f} s"
    )
    return out


def _where(verdict: Verdict, here: str) -> str:
    """Return the mutant's file, relative to *here*, and line, as `path:line`."""
    return f"{os.path.relpath(verdict.mutant.path, here)}:{verdict.mutant.line}"


def _said(verdict: Verdict) -> str:
    """Return the verdict as the console report words it."""
    if verdict.verdict == "killed":
        return f"killed by {verdict.killer} ({verdict.when})"
    if verdict.verdict == "survived":
        return f"SURVIVED {verdict.tests} test{'' if verdict.tests == 1 else 's'}"
    return f"{verdict.verdict.upper()}: {verdict.note}"


def write_json(report: Report, path: str, here: str) -> None:
    """Write every mutant with its verdict, as JSON, to the file *path*.

    Paths in the file are relative to *here*.
    """
    data = {
        "targets": [os.path.relpath(t, here) for t in report.targets],
        "skipped": [
            {"file": os.path.relpath(path, here), "reason": SKIPPED}
            for path in report.skipped
        ],
        "mutants": [
            {
                "id": v.mutant.id,
                "file": os.path.relpath(v.mutant.path, here),
                "line": v.mutant.line,
                "function": v.mutant.qualname,
                "description": v.mutant.description,
                "verdict": v.verdict,
                "killer": v.killer,
                "when": v.when,
                "tests": v.tests,
                "note": v.note,
            }
            for v in report.verdicts
        ],
        "canary": report.canary,
        "seconds": report.seconds,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
