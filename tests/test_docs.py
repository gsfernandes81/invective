"""The documentation's mechanical rules, read against the tree.

The prose is README.md, CLAUDE.md and every page under docs/. A page that
names a path or a symbol goes on naming it after the code has moved, and
nothing else in the suite reads the page; each check here reads the prose
as data and asks the tree.
"""

from __future__ import annotations

import glob
import importlib
import os
import posixpath
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pages():
    found = ["README.md", "CLAUDE.md"]
    for here, dirs, files in os.walk(os.path.join(ROOT, "docs")):
        dirs.sort()
        rel = os.path.relpath(here, ROOT).replace(os.sep, "/")
        found += [posixpath.join(rel, name) for name in sorted(files)
                  if name.endswith(".md")]
    return found


PAGES = _pages()
DOCS_PAGES = [page for page in PAGES if page.startswith("docs/")]


def _text(page):
    with open(os.path.join(ROOT, *page.split("/")), encoding="utf-8") as fh:
        return fh.read()


_FENCE = re.compile(r"^(`{3,}|~{3,})\s*([\w+-]*)\s*$")


def _split(text):
    """(prose, blocks): the lines outside fenced blocks, each as (number,
    line), and every fenced block as (info string, its lines)."""
    prose, blocks, fence = [], [], None
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


def _paragraphs(text):
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


def _spans(text):
    return [span for para in _paragraphs(text)
            for span in re.findall(r"`([^`]+)`", para)]


# --------------------------------------------------------------------------
# Paths

_PATH_ROOTS = ("src/", "tests/", "docs/", ".github/", ".claude/")
_TOP_FILES = {"README.md", "CLAUDE.md", "LICENSE", "pyproject.toml", "uv.lock"}


def _cited_paths(page):
    cited = []
    for span in _spans(_text(page)):
        span = span.strip()
        if " " in span:
            continue
        if span in _TOP_FILES or span.startswith(_PATH_ROOTS):
            cited.append(span.partition("::")[0])
    for para in _paragraphs(_text(page)):
        for link in re.findall(r"\]\(([^)\s]+)\)", para):
            if not re.match(r"[a-z]+:|#", link):
                link = link.partition("#")[0]
                cited.append(posixpath.normpath(posixpath.join(
                    posixpath.dirname(page), link)))
    return cited


@pytest.mark.parametrize("page", PAGES)
def test_every_path_the_prose_names_exists(page):
    """A renamed or deleted file leaves its old path in the prose, sending a
    reader to nothing."""
    missing = [path for path in _cited_paths(page)
               if not glob.glob(os.path.join(ROOT, *path.rstrip("/").split("/")))]
    assert not missing, "%s names paths that do not exist: %s" % (page, missing)


# --------------------------------------------------------------------------
# Symbols

_MODULES = ("invective", "plugin", "engine", "children", "finder", "instrument",
            "tracer", "switch", "report", "errors", "operators", "fn_value")
_SYMBOL = re.compile(r"(?:%s)(?:\.[A-Za-z_]\w*)+" % "|".join(_MODULES))
_EXTENSIONS = {"py", "md", "toml", "cfg", "ini", "yml", "json", "lock", "txt"}


def _cited_symbols(page):
    return [span for span in _spans(_text(page)) if _SYMBOL.fullmatch(span)
            and span.rpartition(".")[2] not in _EXTENSIONS]


def _resolves(symbol):
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
def test_every_dotted_symbol_resolves(page):
    """A renamed function or constant leaves its old name in the prose, and
    the reader opens the module to find it gone."""
    gone = [symbol for symbol in _cited_symbols(page) if not _resolves(symbol)]
    assert not gone, "%s names symbols that do not resolve: %s" % (page, gone)


# --------------------------------------------------------------------------
# The writing rules docs/documentation.md states


@pytest.mark.parametrize("page", DOCS_PAGES)
def test_a_docs_page_opens_with_the_header(page):
    """`head -4 docs/*.md` is the index, and a page without the header shape
    is missing from it or garbles it."""
    lines = _text(page).splitlines()
    assert re.fullmatch(r"# \S.*", lines[0]), "line 1 is `# <subject>`"
    assert lines[1] == "", "line 2 is blank"
    summary = lines[2:lines.index("", 2)]
    assert 1 <= len(summary) <= 2, "one or two summary lines, then a blank"
    assert not summary[0].startswith(("#", "-", "|", ">", "```"))
    assert summary[-1].endswith("."), "the summary is a sentence"


def _prose_lines(page):
    """Prose lines a person wrapped: not tables, and not a line that is one
    unbreakable token."""
    return [(n, line) for n, line in _split(_text(page))[0]
            if not line.lstrip().startswith("|") and " " in line.strip()]


@pytest.mark.parametrize("page", PAGES)
def test_prose_wraps_at_90(page):
    """A line past 90 characters is one the next edit rewraps, and the diff of
    a one-word change becomes the whole paragraph."""
    long = [n for n, line in _prose_lines(page) if len(line) > 90]
    assert not long, "%s: lines past 90 characters: %s" % (page, long)


_LINE_CITATION = re.compile(r"\.(?:py|md|toml|yml|cfg):\d+|\blines? \d+")


@pytest.mark.parametrize("page", PAGES)
def test_no_line_number_citations(page):
    """A line number goes wrong with the first edit above it, and nothing says
    so; a symbol is cited instead."""
    cited = [n for n, line in _prose_lines(page) if _LINE_CITATION.search(line)]
    assert not cited, "%s: line-number citations on lines %s" % (page, cited)


# Two spaced hyphens, a spaced hyphen or an en or em dash, outside code spans.
_DASH = re.compile(r" --? |–|—")


@pytest.mark.parametrize("page", [page for page in PAGES if page != "CLAUDE.md"])
def test_no_dash_asides(page):
    """docs/documentation.md puts asides in parentheses; CLAUDE.md is the
    owner's wording and is not held to it."""
    dashed = [n for n, line in _prose_lines(page)
              if _DASH.search(re.sub(r"`[^`]*`", "``", line.lstrip("-* ")))]
    assert not dashed, "%s: dash asides on lines %s" % (page, dashed)


# --------------------------------------------------------------------------
# Every scan above finds something

# The symbol floor is 0 until stage 4 writes the README and docs that cite
# the engine's symbols; it rises with them.
_SCANS = {
    "paths": (_cited_paths, 10),
    "symbols": (_cited_symbols, 0),
    "prose lines": (_prose_lines, 200),
}


@pytest.mark.parametrize("scan", sorted(_SCANS))
def test_every_scan_finds_something_to_check(scan):
    """A pattern that stops matching (a new code-block style, a changed citation
    form) passes every check above while checking nothing."""
    find, least = _SCANS[scan]
    found = sum(len(find(page)) for page in PAGES)
    assert found >= least, "%s: %d found, at least %d expected" % (scan, found, least)


def test_the_header_check_sees_every_page():
    assert {"docs/documentation.md", "docs/plans/rewrite.md"} <= set(DOCS_PAGES)
