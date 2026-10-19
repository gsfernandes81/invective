"""The comparison of the three arms, the expected-disagreement file, and the table.

Every mutant is keyed by its target's path, as the project names it, and its
ordinal in that file's table; never by the display id, which two mutants can share.
A disagreement is a mutant on which one of these rules fires:

1. One arm says `killed` and another `survived`.
2. Arm 1 says `unreached` and arm 3 kills.
3. Arm 1 says `unmeasured`, arm 3 gives a plain `killed` or `survived`, and the
   engine's note gives no reason the plan accepts. Accepted: a function that ran at
   import, when arm 3's probe saw it run outside any test; and a run over its wall
   budget, which awaits confirmation. A child that ended without a verdict is
   accepted through arm 3: its kill is then not plain, since its run ended too.
4. Arm 1 refused or crashed while arm 3's baseline is green, or ran while it is red:
   a disagreement for the whole project.
5. Arms 1 and 2 give different verdicts. Arm 2 does not measure reach, so its
   `survived` agrees with arm 1's `unreached`. Arm 1's `unmeasured` is left to rule
   3 and not compared with arm 2: arm 2 measures an import-time mutant that arm 1
   leaves unmeasured by design, so the two differ on every one of them. Rule 3
   checks the import-time note against arm 3's probe instead, which catches a
   function marked import-time that never ran outside a test.
6. Arms 1 and 3 give different verdicts that no rule above covers, such as a kill or
   a survivor where arm 3 finds the function never ran.

The expected-disagreement file of a project is its `expected.toml`; its format is in
`load`'s docstring. The comparison passes when the disagreements found equal the
ones it lists, in both directions.
"""

from __future__ import annotations

import itertools
import os
import tomllib
from typing import NamedTuple

from harness.arms import Verdict

#: The verdict words an arm or an entry may use.
VERDICTS = frozenset(
    {"killed", "survived", "unreached", "unmeasured", "timeout", "refused", "crashed"}
)

#: The arms, as the expected-disagreement file names them.
ARMS = ("engine", "subprocess", "truth")


class Mutant(NamedTuple):
    """One mutant, as the harness keys it."""

    path: str  #: the target file as the project names it, relative to the project
    ordinal: int  #: its index in that file's table, from 1
    function: str  #: its qualname, as the table gives it
    value: str  #: the replacement value, as the table spells it


class Row(NamedTuple):
    """One mutant's verdicts, with arm 1 run with `workers` children."""

    mutant: Mutant
    workers: int
    arms: dict[str, Verdict]  #: by arm name; an arm that gave none is absent
    rules: tuple[str, ...]  #: the rules that fired; empty when the arms agree


class Entry(NamedTuple):
    """One `[[disagreement]]` of an expected-disagreement file."""

    path: str
    ordinal: int | None
    function: str | None
    value: str | None
    workers: int
    arms: dict[str, str]  #: the expected verdict of each arm the entry names
    reason: str


class Expected(NamedTuple):
    """A project's expected-disagreement file, and how the harness runs the project."""

    project: str | None  #: arm 1's expected project-level result
    entries: list[Entry]
    targets: list[str] | None  #: what the arms mutate, when not `src/<name>`
    workers: list[int]  #: further worker counts arm 1 runs with
    links: list[str]  #: files that must be symbolic links, or the project is skipped


_ENTRY_KEYS = frozenset({"path", "ordinal", "function", "value", "workers", "reason"})


def load(path: str) -> Expected:
    """Return the expected-disagreement file *path*; a missing file expects none.

    The file holds three optional parts:

    - `[harness]`: `targets`, the paths the arms mutate, relative to the project
      (default `src/<name>`); `workers`, further worker counts arm 1 runs with,
      besides 1; `links`, files that must be symbolic links, without which the
      project is skipped (a checkout without symbolic links).
    - `[project]`: `engine`, `refused` or `crashed`, and `reason`, for a
      project-level disagreement.
    - `[[disagreement]]`, one per expected disagreement: `path`, the target as the
      project names it; `ordinal`, or `function` with an optional `value` (without
      it, every mutant of the function); `workers`, the worker count of the arm 1
      run it holds for (default 1); `engine`, `subprocess` and `truth`, each arm's
      verdict, an arm left out not being compared; and `reason`.

    Raises `ValueError` for an unknown key, a verdict that is not a verdict word, or
    an entry without a reason or a mutant.
    """
    if not os.path.exists(path):
        return Expected(None, [], None, [], [])
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    _known(data, {"harness", "project", "disagreement"}, path)
    harness = data.get("harness", {})
    _known(harness, {"targets", "workers", "links"}, f"{path} [harness]")
    project = data.get("project")
    if project is not None:
        _known(project, {"engine", "reason"}, f"{path} [project]")
        if project.get("engine") not in ("refused", "crashed") or not project.get(
            "reason"
        ):
            raise ValueError(
                f"{path} [project]: needs engine refused or crashed, and a reason"
            )
    entries = [_entry(item, path) for item in data.get("disagreement", [])]
    return Expected(
        project["engine"] if project else None,
        entries,
        harness.get("targets"),
        list(harness.get("workers", [])),
        list(harness.get("links", [])),
    )


def _known(table: dict, keys: set[str] | frozenset[str], where: str) -> None:
    """Raise `ValueError` if *table* has a key not in *keys*."""
    unknown = set(table) - set(keys)
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")


def _entry(item: dict, where: str) -> Entry:
    """Return the `[[disagreement]]` *item* of the file *where*, checked."""
    _known(item, _ENTRY_KEYS | set(ARMS), f"{where} [[disagreement]]")
    arms = {arm: item[arm] for arm in ARMS if arm in item}
    bad = {verdict for verdict in arms.values() if verdict not in VERDICTS}
    if bad or not item.get("reason") or not item.get("path"):
        raise ValueError(
            f"{where}: an entry needs a path, a reason and verdict words: {item}"
        )
    if ("ordinal" in item) == ("function" in item) or (
        "value" in item and "function" not in item
    ):
        raise ValueError(f"{where}: an entry names an ordinal, or a function: {item}")
    return Entry(
        item["path"],
        item.get("ordinal"),
        item.get("function"),
        item.get("value"),
        item.get("workers", 1),
        arms,
        item["reason"],
    )


def names(entry: Entry, mutant: Mutant) -> bool:
    """Return whether *entry* names *mutant*."""
    if entry.path != mutant.path:
        return False
    if entry.ordinal is not None:
        return entry.ordinal == mutant.ordinal
    return entry.function == mutant.function and entry.value in (None, mutant.value)


def _accepted(note: str, outside: bool) -> bool:
    """Return whether arm 1's unmeasured *note* gives a reason the plan accepts.

    *outside* is whether arm 3's probe saw the function run outside any test.
    """
    if "ran at import" in note:
        return outside
    return "over its wall budget" in note


def rules(arms: dict[str, Verdict], outside: bool) -> tuple[str, ...]:
    """Return the rules that fire on one mutant's verdicts *arms*, by arm name.

    *outside* is whether arm 3's probe saw the function run outside any test.
    """
    fired = []
    for (a, first), (b, second) in itertools.combinations(arms.items(), 2):
        if {first.verdict, second.verdict} == {"killed", "survived"}:
            fired.append(f"1: {a} {first.verdict}, {b} {second.verdict}")
    engine, sub, truth = (arms.get(arm) for arm in ARMS)
    if engine is not None and truth is not None:
        pair = {engine.verdict, truth.verdict}
        if engine.verdict == "unmeasured":
            if (
                truth.plain
                and truth.verdict in ("killed", "survived")
                and not _accepted(engine.note, outside)
            ):
                fired.append("3: unmeasured, for no reason the plan accepts")
        elif engine.verdict == "unreached" and truth.verdict == "killed":
            fired.append("2: a false unreached")
        elif engine.verdict != truth.verdict and pair != {"killed", "survived"}:
            fired.append(f"6: engine {engine.verdict}, truth {truth.verdict}")
    if (
        engine is not None
        and sub is not None
        and engine.verdict not in ("unmeasured", sub.verdict)
        and (engine.verdict, sub.verdict) != ("unreached", "survived")
        and {engine.verdict, sub.verdict} != {"killed", "survived"}
    ):
        fired.append(f"5: engine {engine.verdict}, subprocess {sub.verdict}")
    return tuple(fired)


def _matches(entry: Entry, row: Row) -> bool:
    """Return whether *entry* expects the disagreement of *row*, verdicts included."""
    return (
        names(entry, row.mutant)
        and entry.workers == row.workers
        and all(
            arm in row.arms and row.arms[arm].verdict == verdict
            for arm, verdict in entry.arms.items()
        )
    )


def unmatched(
    rows: list[Row], project: str | None, expected: Expected, table: list[Mutant]
) -> list[str]:
    """Return every difference between the disagreements found and those expected.

    *rows* are the mutants' rows, *project* arm 1's project-level disagreement or
    None, and *table* every mutant. An entry that names no mutant of *table* is a
    difference too.
    """
    found = []
    if project != expected.project:
        found.append(
            f"project: arm 1's project-level disagreement is {project or 'none'}, "
            f"expected {expected.project or 'none'}"
        )
    found += [
        f"unexpected: {_label(row)}: {'; '.join(row.rules)}"
        for row in rows
        if row.rules and not any(_matches(entry, row) for entry in expected.entries)
    ]
    by_key = {(row.mutant, row.workers): row for row in rows}
    for entry in expected.entries:
        named = [mutant for mutant in table if names(entry, mutant)]
        if not named:
            found.append(f"expected entry names no mutant: {entry}")
        for mutant in named:
            row = by_key.get((mutant, entry.workers))
            if row is None or not row.rules or not _matches(entry, row):
                seen = _cells(row) if row is not None else "no run"
                found.append(
                    f"expected, not seen: {mutant.path}:{mutant.ordinal} "
                    f"{mutant.function} {mutant.value} (workers {entry.workers}) "
                    f"{entry.arms}, found {seen}"
                )
    return found


def _label(row: Row) -> str:
    """Return a row's mutant and worker count, for a message."""
    mutant = row.mutant
    return (
        f"{mutant.path}:{mutant.ordinal} {mutant.function} {mutant.value} "
        f"(workers {row.workers})"
    )


def _cell(verdict: Verdict | None) -> str:
    """Return one arm's verdict as a table cell."""
    if verdict is None:
        return "-"
    if verdict.verdict == "killed" and verdict.killer:
        return f"killed by {verdict.killer.rpartition('::')[2] or verdict.killer}"
    return verdict.verdict


def _cells(row: Row) -> str:
    """Return a row's three verdicts, for a message."""
    return ", ".join(f"{arm} {_cell(row.arms.get(arm))}" for arm in ARMS)


def render(title: str, lines: list[str], rows: list[Row], differences: list[str]) -> str:
    """Return the project's table: its header *lines*, a row per mutant, the notes.

    The differences between what was found and what was expected follow the notes.
    """
    header = ("mutant", "function", "value", "workers", *ARMS, "rules")
    body = [
        (
            f"{row.mutant.path}:{row.mutant.ordinal}",
            row.mutant.function,
            row.mutant.value,
            str(row.workers),
            *(_cell(row.arms.get(arm)) for arm in ARMS),
            " | ".join(row.rules) or "agree",
        )
        for row in rows
    ]
    widths = [max(len(cells[i]) for cells in [header, *body]) for i in range(len(header))]
    out = [title, *lines, ""]
    out += [
        "  ".join(
            cell.ljust(width) for cell, width in zip(cells, widths, strict=True)
        ).rstrip()
        for cells in [header, *body]
    ]
    notes = [
        f"{row.mutant.path}:{row.mutant.ordinal} (workers {row.workers}) {arm}: "
        + verdict.note.splitlines()[0]
        for row in rows
        for arm, verdict in row.arms.items()
        if verdict.note and verdict.verdict != "killed"
    ]
    if notes:
        out += ["", "notes:", *notes]
    if differences:
        out += ["", "against the expected disagreements:", *differences]
    return "\n".join(out)
