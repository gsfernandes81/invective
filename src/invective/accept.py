"""Survivors accepted in the source, beside the code they are about.

    out[-400:]  # invective: accept[equivalent: 400 -> 401] any length serves

    # invective: accept[untestable] Windows only; the sweep runs on Linux
    subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)

The reason is one of `REASONS`, and a justification after it is required. A
change after the reason (`400 -> 401`, as the report names it) accepts that
mutant of the line and no other; without one, every mutant of the line is
accepted. A comment on a line of its own applies to the next line that is not
itself such a comment, so several can stand above one line of code.

An acceptance is a claim that a mutant survives and that this is all right.
It is **stale**, and said so, when the claim is false: when a mutant it
accepts is killed, or when no mutant of the line is one it could accept.
"""

from __future__ import annotations

import io
import re
import tokenize
from typing import NamedTuple

from invective.errors import Refusal

#: The same three as pytest-gremlins' pardons.
REASONS = ("equivalent", "untestable", "out_of_scope")

_MARK = re.compile(r"#\s*invective\s*:")
_ACCEPT = re.compile(r"#\s*invective\s*:\s*accept\[(?P<reason>[^\]:]*)"
                     r"(?::(?P<change>[^\]]*))?\]\s*(?P<why>.*)$")


class Accept(NamedTuple):
    line: int            #: the line of code it applies to
    at: int              #: the line it is written on
    reason: str
    change: str | None   #: the one mutant it accepts, or every one when None
    why: str

    def covers(self, line: int, change: str) -> bool:
        return line == self.line and self.change in (None, change)


def read(source: str, path: str) -> list[Accept]:
    """Every acceptance in *source*, the file at *path*."""
    found = []
    standalone = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and _MARK.match(tok.string):
                at = tok.start[0]
                if tok.line[:tok.start[1]].strip():
                    found.append(Accept(at, at, *_parse(tok.string, at, path)))
                else:
                    standalone.append((at, _parse(tok.string, at, path)))
    except (tokenize.TokenError, SyntaxError) as exc:
        raise Refusal("%s cannot be read for its comments: %s"
                      % (path, exc)) from exc
    standing = {at for at, _parts in standalone}
    for at, parts in standalone:
        line = at + 1
        while line in standing:
            line += 1
        found.append(Accept(line, at, *parts))
    return sorted(found, key=lambda a: (a.line, a.at))


def _parse(comment: str, at: int, path: str) -> tuple[str, str | None, str]:
    """The reason, the change if one is named, and the justification."""
    got = _ACCEPT.match(comment)
    if got is None:
        raise Refusal("%s:%d: an invective comment says `invective: "
                      "accept[reason] why` or nothing: %s" % (path, at, comment))
    reason = got["reason"].strip()
    if reason not in REASONS:
        raise Refusal("%s:%d: %r is no reason to accept a survivor; the "
                      "reasons are %s" % (path, at, reason, ", ".join(REASONS)))
    why = got["why"].strip()
    if not why:
        raise Refusal("%s:%d: an acceptance says why the mutant may survive"
                      % (path, at))
    change = got["change"]
    if change is not None:
        change = change.strip()
        if not change:
            raise Refusal("%s:%d: a colon after the reason names the one "
                          "mutant accepted, as the report gives its change"
                          % (path, at))
    return reason, change, why
