"""The documentation's mechanical rules, checked against the tree.

The prose is README.md, CLAUDE.md and every page under docs/. A page that names a
path or a symbol goes on naming it after the code has moved, and nothing else in the
suite reads the page; each check here reads the prose as data and looks the name up
in the tree.
"""

from __future__ import annotations

import glob
import importlib
import os
import posixpath
import re
from collections.abc import Callable

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pages() -> list[str]:
    """Return every page of prose, as a path from the root with `/` separators."""
    found = ["README.md", "CLAUDE.md"]
    for here, dirs, files in os.walk(os.path.join(ROOT, "docs")):
        dirs.sort()
        rel = os.path.relpath(here, ROOT).replace(os.sep, "/")
        found += [
            posixpath.join(rel, name) for name in sorted(files) if name.endswith(".md")
        ]
    return found


PAGES = _pages()
DOCS_PAGES = [page for page in PAGES if page.startswith("docs/")]


def _text(page: str) -> str:
    """Return the text of *page*."""
    with open(os.path.join(ROOT, *page.split("/")), encoding="utf-8") as fh:
        return fh.read()


_FENCE = re.compile(r"^(`{3,}|~{3,})\s*([\w+-]*)\s*$")


def _split(
    text: str,
) -> tuple[list[tuple[int, str]], list[tuple[str, list[str]]]]:
    """Return the prose lines of *text* and its fenced blocks.

    The prose is every line outside a fenced block, as (number, line); each block
    is (info string, its lines). Fails on a fence left open.
    """
    prose: list[tuple[int, str]] = []
    blocks: list[tuple[str, list[str]]] = []
    fence, info, body = None, "", []
    for number, line in enumerate(text.splitlines(), 1):
        match = _FENCE.match(line)
        if fence is None and match:
            fence, info, body = match.group(1), match.group(2), []
        elif fence is not None:
            if line.strip() == fence:
                blocks.append((info, body))
                fence = None
            else:
                body.append(line)
        else:
            prose.append((number, line))
    assert fence is None, "an unclosed fence"
    return prose, blocks


def _paragraphs(text: str) -> list[str]:
    """Return the prose paragraphs of *text*, each joined onto one line."""
    out, current = [], []
    for _number, line in _split(text)[0]:
        if line.strip():
            current.append(line.strip())
        elif current:
            out.append(" ".join(current))
            current = []
    if current:
        out.append(" ".join(current))
    return out


def _spans(text: str) -> list[str]:
    """Return the text of every code span in the prose of *text*."""
    return [span for para in _paragraphs(text) for span in re.findall(r"`([^`]+)`", para)]


# --------------------------------------------------------------------------
# Paths

_PATH_ROOTS = ("src/", "tests/", "docs/", ".github/", ".claude/")
_TOP_FILES = {"README.md", "CLAUDE.md", "LICENSE", "pyproject.toml", "uv.lock"}


def _cited_paths(page: str) -> list[str]:
    """Return every path *page* names, in a code span or as a relative link."""
    cited = []
    for span in _spans(_text(page)):
        name = span.strip()
        if " " in name:
            continue
        if name in _TOP_FILES or name.startswith(_PATH_ROOTS):
            cited.append(name.partition("::")[0])
    for para in _paragraphs(_text(page)):
        for link in re.findall(r"\]\(([^)\s]+)\)", para):
            if not re.match(r"[a-z]+:|#", link):
                target = link.partition("#")[0]
                cited.append(
                    posixpath.normpath(posixpath.join(posixpath.dirname(page), target))
                )
    return cited


@pytest.mark.parametrize("page", PAGES)
def test_every_path_the_prose_names_exists(page: str) -> None:
    """Every path a page names exists.

    A renamed or deleted file leaves its old path in the prose, sending a reader to
    nothing.
    """
    missing = [
        path
        for path in _cited_paths(page)
        if not glob.glob(os.path.join(ROOT, *path.rstrip("/").split("/")))
    ]
    assert not missing, f"{page} names paths that do not exist: {missing}"


# --------------------------------------------------------------------------
# Symbols

_MODULES = (
    "invective",
    "plugin",
    "engine",
    "baseline",
    "mutants",
    "protocol",
    "children",
    "environment",
    "finder",
    "instrument",
    "tracer",
    "switch",
    "report",
    "errors",
    "operators",
    "fn_value",
)
_SYMBOL = re.compile(rf"(?:{'|'.join(_MODULES)})(?:\.[A-Za-z_]\w*)+")
_EXTENSIONS = {"py", "md", "toml", "cfg", "ini", "yml", "json", "lock", "txt"}


def _cited_symbols(page: str) -> list[str]:
    """Return every dotted name of invective's that *page* names in a code span."""
    return [
        span
        for span in _spans(_text(page))
        if _SYMBOL.fullmatch(span) and span.rpartition(".")[2] not in _EXTENSIONS
    ]


def _resolves(symbol: str) -> bool:
    """Return whether *symbol* names a module of invective's or an attribute in one."""
    parts = symbol.split(".")
    if parts[0] != "invective":
        parts.insert(0, "invective")
    if parts[1] == "fn_value":
        parts.insert(1, "operators")
    for cut in range(len(parts), 0, -1):
        try:
            found = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for name in parts[cut:]:
            if not hasattr(found, name):
                return False
            found = getattr(found, name)
        return True
    return False


@pytest.mark.parametrize("page", PAGES)
def test_every_dotted_symbol_resolves(page: str) -> None:
    """Every dotted name of invective's that a page names exists.

    A renamed function or constant leaves its old name in the prose, and the reader
    opens the module to find it gone.
    """
    gone = [symbol for symbol in _cited_symbols(page) if not _resolves(symbol)]
    assert not gone, f"{page} names symbols that do not resolve: {gone}"


# --------------------------------------------------------------------------
# The writing rules docs/documentation.md states


@pytest.mark.parametrize("page", DOCS_PAGES)
def test_a_docs_page_opens_with_the_header(page: str) -> None:
    """A page under docs/ opens with a title, a blank line and a summary sentence.

    `head -4 docs/*.md` is the index, and a page without the header shape is
    missing from it or garbles it.
    """
    lines = _text(page).splitlines()
    assert re.fullmatch(r"# \S.*", lines[0]), "line 1 is `# <subject>`"
    assert lines[1] == "", "line 2 is blank"
    summary = lines[2 : lines.index("", 2)]
    assert 1 <= len(summary) <= 2, "one or two summary lines, then a blank"
    assert not summary[0].startswith(("#", "-", "|", ">", "```"))
    assert summary[-1].endswith("."), "the summary is a sentence"


def _prose_lines(page: str) -> list[tuple[int, str]]:
    """Return the prose lines of *page* a person wrapped, as (number, line).

    A table row is left out, and so is a line that is one unbreakable token.
    """
    return [
        (n, line)
        for n, line in _split(_text(page))[0]
        if not line.lstrip().startswith("|") and " " in line.strip()
    ]


@pytest.mark.parametrize("page", PAGES)
def test_prose_wraps_at_90(page: str) -> None:
    """No prose line is past 90 characters.

    A longer line is one the next edit rewraps, and the diff of a one-word change
    becomes the whole paragraph.
    """
    long = [n for n, line in _prose_lines(page) if len(line) > 90]
    assert not long, f"{page}: lines past 90 characters: {long}"


_LINE_CITATION = re.compile(r"\.(?:py|md|toml|yml|cfg):\d+|\blines? \d+")


@pytest.mark.parametrize("page", PAGES)
def test_no_line_number_citations(page: str) -> None:
    """No page cites a line number.

    A line number goes wrong with the first edit above it, and nothing reports it;
    a symbol is cited instead.
    """
    cited = [n for n, line in _prose_lines(page) if _LINE_CITATION.search(line)]
    assert not cited, f"{page}: line-number citations on lines {cited}"


# Two spaced hyphens, a spaced hyphen, or an en or em dash, outside code spans.
_DASH = re.compile(r" --? |\u2013|\u2014")


@pytest.mark.parametrize("page", [page for page in PAGES if page != "CLAUDE.md"])
def test_no_dash_asides(page: str) -> None:
    """No page but CLAUDE.md sets an aside off with dashes.

    docs/documentation.md puts asides in parentheses; CLAUDE.md is the owner's
    wording and is not held to it.
    """
    dashed = [
        n
        for n, line in _prose_lines(page)
        if _DASH.search(re.sub(r"`[^`]*`", "``", line.lstrip("-* ")))
    ]
    assert not dashed, f"{page}: dash asides on lines {dashed}"


# --------------------------------------------------------------------------
# Every scan above finds something

# The symbol floor is 0 until stage 4 writes the README and docs that cite the
# engine's symbols; it rises with them.
_SCANS: dict[str, tuple[Callable[[str], list], int]] = {
    "paths": (_cited_paths, 10),
    "symbols": (_cited_symbols, 0),
    "prose lines": (_prose_lines, 200),
}


@pytest.mark.parametrize("scan", sorted(_SCANS))
def test_every_scan_finds_something_to_check(scan: str) -> None:
    """Each scan finds at least its floor of items across the pages.

    A pattern that stops matching (a new code-block style, a changed citation form)
    passes every check above while checking nothing.
    """
    find, least = _SCANS[scan]
    found = sum(len(find(page)) for page in PAGES)
    assert found >= least, f"{scan}: {found} found, at least {least} expected"


def test_the_header_check_sees_every_page() -> None:
    """The header check reads docs/ and its plans directory."""
    assert {"docs/documentation.md", "docs/plans/rewrite.md"} <= set(DOCS_PAGES)
