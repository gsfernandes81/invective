"""What a run says: the console lines and the JSON file."""

from __future__ import annotations

import json
import os

from invective.engine import Report, Verdict

#: The verdicts, in the order the summary counts them.
VERDICTS = ("killed", "survived", "unreached", "unmeasured")


def lines(report: Report, here: str) -> list[str]:
    """The console report: one line per mutant, then the counts."""
    out = []
    width = max((len(_where(v, here)) for v in report.verdicts), default=0)
    for verdict in report.verdicts:
        out.append("%-*s  %s  %s" % (width, _where(verdict, here), verdict.mutant.description,
                                     _said(verdict)))
    counts = {name: sum(1 for v in report.verdicts if v.verdict == name)
              for name in VERDICTS}
    # An unreached mutant counts as a survivor; an unmeasured one is neither.
    scored = counts["killed"] + counts["survived"] + counts["unreached"]
    score = ("%.1f%%, %d killed of %d" % (100.0 * counts["killed"] / scored,
                                          counts["killed"], scored)
             if scored else "none, nothing to score")
    out.append("canary killed by %s" % report.canary)
    out.append("%d mutants in %d files: %s; score %s; %.1f s" % (
        len(report.verdicts), len(report.targets),
        ", ".join("%d %s" % (counts[name], name) for name in VERDICTS), score,
        report.seconds))
    return out


def _where(verdict: Verdict, here: str) -> str:
    return "%s:%d" % (os.path.relpath(verdict.mutant.path, here), verdict.mutant.line)


def _said(verdict: Verdict) -> str:
    if verdict.verdict == "killed":
        return "killed by %s (%s)" % (verdict.killer, verdict.when)
    if verdict.verdict == "survived":
        return "SURVIVED %d test%s" % (verdict.tests, "" if verdict.tests == 1 else "s")
    return "%s: %s" % (verdict.verdict.upper(), verdict.note)


def write_json(report: Report, path: str, here: str) -> None:
    data = {
        "targets": [os.path.relpath(t, here) for t in report.targets],
        "mutants": [{
            "id": v.mutant.id, "file": os.path.relpath(v.mutant.path, here),
            "line": v.mutant.line, "function": v.mutant.qualname,
            "description": v.mutant.description, "verdict": v.verdict,
            "killer": v.killer, "when": v.when, "tests": v.tests, "note": v.note,
        } for v in report.verdicts],
        "canary": report.canary,
        "seconds": report.seconds,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
