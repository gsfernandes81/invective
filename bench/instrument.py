"""One invective command, timed from the inside, adding nothing to the product.

    python instrument.py --calls CALLS.jsonl --marks MARKS.json run ARGS...
    python instrument.py --calls CALLS.jsonl --marks MARKS.json pytest ARGS...

`run` is `invective run ARGS`; `pytest` is `python -m pytest ARGS`, the
plugin's way in. Either way, in this process only (never in the runs it
starts), every `Copy.run` appends one JSON line to CALLS: the copy, the target, the
mutant's place, kind, line, change and mark (`mark`; null for a run of the
original), the
selection file's prefix (`""` for the campaign's own), whether it carried the
coverage handshake, its seconds and what it came to. MARKS gets the seconds
from the start at which the campaign said its `baseline:` line (`baseline`),
closed its pool (`pool`), first restored a copy (`confirm_start`) and
returned its report (`end`).

It runs under the side's own interpreter, against whichever invective that
venv holds, so every hook is taken only where the version has it: an
invective without `Copy` writes no calls, and the caller reads that as "no
instrument".
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import threading
import time

_START = time.monotonic()
_lock = threading.Lock()
_marks: dict[str, float] = {}


def _mark(name: str) -> None:
    _marks.setdefault(name, round(time.monotonic() - _START, 3))


def _prefix(selection: object) -> str:
    """`narrowed-`, `probe-` or `killer-` for a selection file invective
    wrote, its name for any other, and "" for none."""
    if not isinstance(selection, str):
        return ""
    name = os.path.basename(selection)
    found = re.match(r"([a-z]+-)\d+\.txt$", name)
    return found.group(1) if found else name


def mark(source: str, text: str, split=str.splitlines) -> str:
    """The mutant's mark, as `compare.mutant_mark` makes it from the
    report's diff: a hash of the lines the mutant takes out of *source* and
    puts in, each split as the report splits them (*split*)."""
    diff = difflib.unified_diff(split(source), split(text), lineterm="")
    changed = [line for line in list(diff)[2:] if line[:1] in ("+", "-")]
    return hashlib.sha1("\n".join(changed).encode("utf-8")).hexdigest()[:16]


def install(calls: str) -> None:
    from invective import mutate as m

    real_mutate = getattr(m, "mutate", None)
    if real_mutate is not None:
        def mutate(*args, **kwargs):
            say = kwargs.get("say", print)

            def said(line, *rest, **more):
                if isinstance(line, str) and line.startswith("baseline:"):
                    _mark("baseline")
                return say(line, *rest, **more)

            kwargs["say"] = said
            try:
                return real_mutate(*args, **kwargs)
            finally:
                _mark("end")

        m.mutate = mutate

    pool = getattr(m, "_Pool", None)
    if pool is not None and hasattr(pool, "close"):
        real_close = pool.close

        def close(self, *args, **kwargs):
            _mark("pool")
            return real_close(self, *args, **kwargs)

        pool.close = close

    copy = getattr(m, "Copy", None)
    if copy is None:
        return
    if hasattr(copy, "restore"):
        real_restore = copy.restore

        def restore(self, *args, **kwargs):
            _mark("confirm_start")
            return real_restore(self, *args, **kwargs)

        copy.restore = restore

    real_run = copy.run
    # The report's own splitting where the engine has it, so the lines
    # hashed are the lines its diff shows.
    split = getattr(m, "_report_lines", str.splitlines)

    def run(self, mutant, *args, **kwargs):
        started = time.monotonic()
        got = None
        try:
            got = real_run(self, mutant, *args, **kwargs)
            return got
        finally:
            job = getattr(mutant, "job", None)
            text, source = getattr(mutant, "text", None), getattr(self, "source", None)
            selection = kwargs.get("selection", args[1] if len(args) > 1 else None)
            # `rel` is the report's `target`, spelt as the campaign spells it.
            row = {"copy": getattr(self, "where", None),
                   "target": getattr(self, "rel", None),
                   "n": getattr(job, "n", None),
                   "kind": getattr(job, "kind", None),
                   "line": getattr(job, "line", None),
                   "change": getattr(job, "what", None),
                   "mark": (mark(source, text, split)
                            if isinstance(text, str) and isinstance(source, str)
                            else None),
                   "prefix": _prefix(selection),
                   "coverage": "coverage" in kwargs,
                   "seconds": round(time.monotonic() - started, 3),
                   "code": getattr(got, "code", None),
                   "killer": getattr(got, "killer", None)}
            with _lock, open(calls, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")

    copy.run = run


def main(argv: list[str]) -> int:
    if len(argv) < 5 or argv[0] != "--calls" or argv[2] != "--marks":
        print(__doc__, file=sys.stderr)
        return 2
    calls, marks, how, rest = argv[1], argv[3], argv[4], argv[5:]
    # This file's directory is not the command's: `invective run` puts its
    # script's on the path, and `python -m pytest` the directory it starts
    # in, where a project's own top-level modules are found.
    del sys.path[0]
    if how == "pytest":
        sys.path.insert(0, os.getcwd())
    open(calls, "w").close()
    install(calls)
    try:
        if how == "run":
            from invective import mutate as m

            return m.main(rest)
        if how == "pytest":
            import pytest

            return int(pytest.main(rest))
        print("instrument: no way in named %r" % how, file=sys.stderr)
        return 2
    finally:
        with open(marks, "w", encoding="utf-8") as fh:
            json.dump(_marks, fh)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
