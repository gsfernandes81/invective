"""The text edits arm 3 makes to a copy of a target: a mutant, or a probe per function.

A mutant is applied by the meaning the instrumenter's table gives it: the function's
running statements, from the first statement after its docstring and its `global`
and `nonlocal` declarations to the end of the function, are replaced by the mutant's
statements, and the `def` line, the decorators, the docstring and the declarations
stay as written. A body that starts on the line of the text before it (a one-line
function, or a body after a docstring and a semicolon) takes its statements on that
line, joined by semicolons; any other takes them on lines of their own at the body's
indentation.

A probe is one expression statement put ahead of a function's running statements,
which creates a file named for the function the first time the function runs in a
process, with a suffix telling whether a test was running (`PYTEST_CURRENT_TEST`
was set). It names no global, so a local that shadows a builtin cannot break it, and
it runs in forked processes and in interpreters a test starts as well.
"""

from __future__ import annotations

import ast
import os
from typing import NamedTuple

#: The suffix of a probe's file for a call made while a test ran, and for any other.
IN_TEST = "t"
OUTSIDE = "i"


class Site(NamedTuple):
    """Where a function's running statements are, in its file's bytes."""

    start: int  #: the offset of the first running statement, decorators included
    end: int  #: the offset just past the function's last statement
    inline: bool  #: the first running statement shares its line with earlier text
    indent: bytes  #: the first running statement's indentation, for a block body
    generator: bool  #: the function yields


def _functions(
    node: ast.AST, scope: list[str]
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return every function under *node* with its qualname, as `instrument` builds it."""
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            found += _functions(child, [*scope, child.name])
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append((".".join([*scope, child.name]), child))
            found += _functions(child, [*scope, child.name, "<locals>"])
        else:
            found += _functions(child, scope)
    return found


def _running(node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.stmt:
    """Return the first statement of *node* after its docstring and declarations."""
    body = node.body
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        body = body[1:]
    lead = 0
    while lead < len(body) and isinstance(body[lead], (ast.Global, ast.Nonlocal)):
        lead += 1
    return (body[lead:] or body)[0]


def _yields(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return whether *node* yields, outside any function, lambda or class in it."""
    pending: list[ast.AST] = list(node.body)
    while pending:
        current = pending.pop()
        if isinstance(current, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            pending.extend(ast.iter_child_nodes(current))
    return False


def sites(source: bytes) -> dict[tuple[str, int, int], Site]:
    """Return every function of *source* by (qualname, first running line, last line).

    The key is the one a mutant of the instrumenter's table carries as its qualname
    and span.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    found = {}
    for qualname, node in _functions(tree, []):
        first = _running(node)
        line, col = first.lineno, first.col_offset
        decorators = getattr(first, "decorator_list", [])
        if decorators:
            line = decorators[0].lineno
            col = lines[line - 1].rindex(b"@", 0, decorators[0].col_offset)
        before = lines[line - 1][:col]
        end_line = node.end_lineno or node.lineno
        found[(qualname, first.lineno, end_line)] = Site(
            start=starts[line - 1] + col,
            end=starts[end_line - 1] + (node.end_col_offset or 0),
            inline=bool(before.strip()),
            indent=before if not before.strip() else b"",
            generator=_yields(node),
        )
    return found


def statements(value: str, generator: bool) -> list[str]:
    """Return the statements of the mutant whose replacement value is *value*.

    For a generator the value is the table's `iter([])` or `iter([v])`, and the
    statements yield `v` or nothing and return; an unreachable `yield` after the
    `return` keeps an empty one a generator.
    """
    if not generator:
        return [f"return {value}"]
    inner = value.removeprefix("iter([").removesuffix("])")
    return ["return", "yield"] if not inner else [f"yield {inner}", "return"]


def _joined(site: Site, lines: list[str]) -> bytes:
    """Return *lines* as one text for *site*: on its line or at its indentation."""
    separator = b"; " if site.inline else b"\n" + site.indent
    return separator.join(line.encode() for line in lines)


def mutate(source: bytes, site: Site, value: str) -> bytes:
    """Return *source* with the function at *site* replaced by the mutant of *value*."""
    replacement = _joined(site, statements(value, site.generator))
    return source[: site.start] + replacement + source[site.end :]


def probe(directory: str, name: str) -> str:
    """Return the probe statement that creates `<name>_<suffix>` in *directory*.

    The suffix is `IN_TEST` or `OUTSIDE`. The file is created once per process and
    suffix.
    """
    flags = os.O_WRONLY | os.O_CREAT
    return (
        "(lambda s, o, k: k in s.__dict__ or s.__dict__.__setitem__(k, "
        f"o.close(o.open({directory!r} + '/' + k, {flags}))))"
        f"(__import__('sys'), __import__('os'), '_invective_probe_{name}_' + "
        f"('{IN_TEST}' if __import__('os').environ.get('PYTEST_CURRENT_TEST') "
        f"else '{OUTSIDE}'))"
    )


def probed(source: bytes, found: dict[str, Site], directory: str) -> bytes:
    """Return *source* with a probe ahead of each site of *found*, under its name."""
    for name, site in sorted(found.items(), key=lambda item: -item[1].start):
        text = _joined(site, [probe(directory, name), ""])
        source = source[: site.start] + text + source[site.start :]
    return source


def probe_file(name: str, suffix: str) -> str:
    """Return the name of the file the probe *name* creates for *suffix*."""
    return f"_invective_probe_{name}_{suffix}"
