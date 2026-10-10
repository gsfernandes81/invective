"""The documentation's checkable facts, read against the code.

A page that names a file, a symbol, a command line or a roster of the code goes
on saying it after the code has moved, and nothing else in the suite reads the
page. Each check here reads the prose as data and asks the code, so a stale
fact is a red test in the pull request that made it stale.

The prose is README.md, CLAUDE.md and every page under docs/, found by walking
the filesystem: a copy invective makes of its own project has no `.git`, and
this file is in the selection of any module it imports.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import glob
import importlib
import io
import os
import posixpath
import re
import shlex
import textwrap
import tomllib

import pytest

import pytest_invective
from invective import __main__ as cli
from invective import accept, config, mutate, sweep, tree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pages():
    """Every documentation file, as a path from the top with `/` separators."""
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
    # An unclosed fence would hide the rest of the page from every check.
    assert fence is None, "an unclosed fence"
    return prose, blocks


def _paragraphs(text):
    """The prose's paragraphs, each joined into one line, as markdown joins a
    wrapped line: a code span the wrap broke in two is read whole."""
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


def _section(page, heading):
    """The text of *page* under *heading*, up to the next heading of its level
    or above, so a check never reads a table that belongs to another one."""
    lines = _text(page).splitlines()
    level = len(heading) - len(heading.lstrip("#"))
    # Headings are found in the prose only: a `#` comment in a fenced block
    # would otherwise end the section in the middle of an example.
    heads = [n - 1 for n, line in _split(_text(page))[0]
             if re.match(r"#{1,%d} " % level, line)]
    start = lines.index(heading) + 1
    end = next((n for n in heads if n >= start), len(lines))
    return "\n".join(lines[start:end])


# --------------------------------------------------------------------------
# Paths

_PATH_ROOTS = ("src/", "tests/", "docs/", ".github/", "bench/")
_TOP_FILES = {"README.md", "CLAUDE.md", "LICENSE", "pyproject.toml", "uv.lock"}


def _cited_paths(page):
    """The repository paths *page* cites in its prose: backticked spans under
    one of the tree's roots or naming a top-level file, and relative links."""
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

_SYMBOL = re.compile(r"(?:invective|pytest_invective|__main__|mutate|sweep|tree"
                     r"|accept|config|errors|process|store)(?:\.[A-Za-z_]\w*)+")
_EXTENSIONS = {"py", "md", "toml", "cfg", "ini", "yml", "json", "lock", "txt"}


def _cited_symbols(page):
    return [span for span in _spans(_text(page)) if _SYMBOL.fullmatch(span)
            and span.rpartition(".")[2] not in _EXTENSIONS]


def _resolves(symbol):
    parts = symbol.split(".")
    if parts[0] not in ("invective", "pytest_invective"):
        parts.insert(0, "invective")
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
# Command lines

_SHELL = {"", "console", "shell", "sh", "bash"}


def _command_lines(page):
    """Every command line of *page*'s shell blocks, split as a shell would,
    with a prompt, a continuation and a leading `uv run` taken away."""
    lines = []
    for info, body in _split(_text(page))[1]:
        if info not in _SHELL:
            continue
        joined = "\n".join(body).replace("\\\n", " ")
        for line in joined.splitlines():
            line = line.strip().removeprefix("$ ")
            if not line:
                continue
            argv = shlex.split(line)
            if argv[:2] == ["uv", "run"]:
                argv = argv[2:]
            if argv[:3] == ["python", "-m", "pytest"]:
                argv = ["pytest"] + argv[3:]
            lines.append(argv)
    return lines


def _invective_lines(page):
    return [argv for argv in _command_lines(page) if argv[:1] == ["invective"]]


def _pytest_mutate_lines(page):
    return [argv for argv in _command_lines(page) if argv[:1] == ["pytest"]
            and any(arg.startswith("--mutate") for arg in argv)]


_PARSERS = {"run": mutate.parser, "sweep": sweep.parser}


def _refusal(ap, argv):
    """What *ap* says refusing *argv*, or None when it parses it."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            ap.parse_args(argv)
    except SystemExit:
        return err.getvalue().strip() or "refused"
    return None


@pytest.mark.parametrize("page", PAGES)
def test_an_invective_command_in_a_code_block_parses(page):
    """A flag renamed or dropped leaves an example that fails as typed, for
    the reader who copies it."""
    for argv in _invective_lines(page):
        assert len(argv) > 1 and argv[1] in cli.COMMANDS, (
            "%s: `%s` is no invective command" % (page, shlex.join(argv)))
        said = _refusal(_PARSERS[argv[1]](), argv[2:])
        assert said is None, "%s: `%s`: %s" % (page, shlex.join(argv), said)


class _Options:
    """What `pytest_invective.pytest_addoption` registers, recorded into an
    argparse parser as pytest's own would take them (no abbreviations)."""

    def __init__(self):
        self.ap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        self.actions = {}
        pytest_invective.pytest_addoption(self)

    def getgroup(self, *_args, **_kwargs):
        return self

    def addoption(self, *names, **kwargs):
        action = self.ap.add_argument(*names, **kwargs)
        self.actions.update(dict.fromkeys(names, action))


@pytest.mark.parametrize("page", PAGES)
def test_a_pytest_mutate_line_uses_only_the_plugins_options(page):
    """A `--mutate-*` option the plugin no longer registers, or one given a
    value it cannot take, makes pytest refuse the example outright."""
    for argv in _pytest_mutate_lines(page):
        options = _Options()
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                _given, rest = options.ap.parse_known_args(argv[1:])
        except SystemExit:
            pytest.fail("%s: `%s`: %s" % (page, shlex.join(argv), err.getvalue()))
        unknown = [arg for arg in rest if arg.startswith("--mutate")]
        assert not unknown, "%s: `%s`: the plugin has no %s" % (
            page, shlex.join(argv), unknown)


def test_USAGE_names_only_flags_the_parsers_have():
    """`__main__.USAGE` is a hand-written copy of both parsers' flags, and a
    flag renamed in a parser stays in it."""
    for line in cli.USAGE.splitlines():
        match = re.search(r"invective (run|sweep)\b(.*)", line)
        if match:
            known = _PARSERS[match.group(1)]()._option_string_actions
            flags = re.findall(r"--[\w-]+", match.group(2))
            assert flags and not set(flags) - set(known), line


# --------------------------------------------------------------------------
# The README's restatements of the code

_CMP_SYMBOL = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
               ast.Eq: "==", ast.NotEq: "!=", ast.In: "in", ast.NotIn: "not in",
               ast.Is: "is", ast.IsNot: "is not"}


def _edits_table():
    rows = re.findall(r"^\| `(\w+)` \| (.*) \|$", _section("README.md", "## The edits"),
                      re.MULTILINE)
    return dict(rows)


def test_the_edits_table_is_OPERATORS():
    """A kind of edit added to or dropped from the engine is one the README's
    table leaves out, or promises and `--only` refuses."""
    assert list(_edits_table()) == list(mutate.OPERATORS)


def test_the_CMP_row_is_the_comparison_swaps():
    """The CMP row spells out every pair of comparisons swapped, so a pair
    added to or dropped from the engine makes it a wrong list."""
    said = {frozenset(pair) for pair in
            re.findall(r"`([^`]+)` and `([^`]+)`", _edits_table()["CMP"])}
    done = {frozenset((_CMP_SYMBOL[a], _CMP_SYMBOL[b]))
            for a, b in mutate._CMP_SWAP.items()}
    assert said == done


def test_the_sweeps_defaults_are_the_parsers():
    """The README states the sweep's default kinds and cap, and a changed
    default would leave a reader expecting the old ones."""
    match = re.search(r"The sweep tries `([A-Z,]+)` only and at most (\d+) mutants",
                      _section("README.md", "## A whole source tree"))
    assert match, "the sentence stating the sweep's defaults"
    ap = sweep.parser()
    assert (match.group(1), int(match.group(2))) == (ap.get_default("only"),
                                                     ap.get_default("limit"))


def test_the_reasons_are_REASONS():
    """A reason added or renamed in `accept.REASONS` leaves the README naming
    one the source comment then refuses."""
    match = re.search(r"The reason is ((?:`\w+`(?:, | or )?)+)",
                      _section("README.md", "## Reading a survivor"))
    assert match, "the sentence naming the reasons"
    assert tuple(re.findall(r"`(\w+)`", match.group(1))) == accept.REASONS


def test_the_README_acceptances_are_read_as_written():
    """An acceptance comment the README shows is one a reader copies into
    their source, so it must be one `accept.read` takes."""
    blocks = [body for info, body in _split(_section("README.md",
                                                     "## Reading a survivor"))[1]
              if info == "python"]
    assert blocks, "the examples of an acceptance"
    for body in blocks:
        source = "\n".join(body) + "\n"
        assert accept.read(source, "example.py"), source


def _settings_examples():
    """The `[tool.invective]` tables the README and `config`'s docstring show."""
    readme = [body for info, body in
              _split(_section("README.md", "## Failing a run on survivors"))[1]
              if info == "toml"]
    doc = config.__doc__.split("\n\n")
    docstring = [block.splitlines() for block in doc
                 if block.strip().startswith("[tool.invective]")]
    return readme + [textwrap.dedent("\n".join(b)).splitlines() for b in docstring]


def test_the_settings_are_config_KEYS(tmp_path):
    """A setting added, renamed or dropped in `config._KEYS` leaves an example
    table that a project copies and has refused, or that misses a key."""
    examples = _settings_examples()
    assert len(examples) == 2, "the README's table and `config`'s docstring's"
    for body in examples:
        text = "\n".join(body) + "\n"
        assert set(tomllib.loads(text)["tool"]["invective"]) == set(config._KEYS)
        (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")
        config.load(str(tmp_path))


def test_the_mutate_options_are_the_plugins():
    """The README pairs each `--mutate-*` option with the `invective run` flag
    it works as, and a new, renamed or retyped one breaks the pairing."""
    match = re.search(r"((?:`--mutate-[\w-]+`(?:, | and )?)+) work as "
                      r"((?:`--[\w-]+`(?:, | and )?)+) do for `invective run`",
                      " ".join(_paragraphs(_section("README.md", "## From pytest"))))
    assert match, "the sentence pairing the plugin's options with run's"
    plugin = re.findall(r"`([^`]+)`", match.group(1))
    run = re.findall(r"`([^`]+)`", match.group(2))
    options = _Options()
    assert set(plugin) == set(options.actions) - {"--mutate"}
    known = mutate.parser()._option_string_actions
    for theirs, ours in zip(plugin, run, strict=True):
        assert theirs == "--mutate-" + ours.removeprefix("--")
        assert options.actions[theirs].type == known[ours].type, theirs


class _Anything:
    """A pytest config for which every option is given: whatever
    `_forwarded` asks of it, it forwards."""

    def __getattr__(self, name):
        return "x"

    def getoption(self, *_args):
        return "x"


def test_the_forwarded_options_are_the_plugins():
    """An option the plugin starts or stops forwarding to each mutant's run
    changes what a run measures, and the README's list is how a user knows."""
    match = re.search(r"These options reach every mutant's run: "
                      r"(.*?)\. `-c FILE` is also forwarded.*?"
                      r"The rest do not reach",
                      " ".join(_paragraphs(_section("README.md", "## From pytest"))))
    assert match, "the sentence listing the forwarded options"
    said = set(re.findall(r"`(-[^`]+)`", match.group(1)))
    config_ = _Anything()
    config_.option = _Anything()
    forwarded = pytest_invective._forwarded(config_)
    assert said == {arg.partition("=")[0] for arg in forwarded if arg.startswith("-")}


def test_the_handshake_is_the_plugins_variables():
    """The architecture page lists the environment variables between engine
    and plugin, and one added or renamed is a handshake the page hides."""
    said = set(re.findall(r"^- `(INVECTIVE_\w+)`:", _section(
        "docs/architecture.md", "## The handshake"), re.MULTILINE))
    named = {value for value in vars(pytest_invective).values()
             if isinstance(value, str) and value.startswith("INVECTIVE_")}
    assert said and said == named


def test_the_skipped_list_is_SKIPPED():
    """The isolation page spells out what a copy leaves out, and a directory
    added to or dropped from `tree.SKIPPED` makes it wrong about the copy."""
    match = re.search(r"What is skipped \(`tree\.SKIPPED`\): (.*?), and any",
                      " ".join(_paragraphs(_section("docs/isolation.md", "## The copy"))))
    assert match, "the sentence listing what is skipped"
    assert set(re.findall(r"`([^`]+)`", match.group(1))) == tree.SKIPPED


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

_SCANS = {
    "paths": (_cited_paths, 20),
    "symbols": (_cited_symbols, 10),
    "invective commands": (_invective_lines, 3),
    "pytest --mutate lines": (_pytest_mutate_lines, 2),
    "prose lines": (_prose_lines, 300),
}


@pytest.mark.parametrize("scan", sorted(_SCANS))
def test_every_scan_finds_something_to_check(scan):
    """A pattern that stops matching (a new code-block style, a changed citation
    form) passes every check above while checking nothing."""
    find, least = _SCANS[scan]
    found = sum(len(find(page)) for page in PAGES)
    assert found >= least, "%s: %d found, at least %d expected" % (scan, found, least)


def test_the_header_check_sees_every_page():
    """The header check is parametrised over the pages the walk finds, and a
    walk that finds none leaves it with nothing to run."""
    assert {"docs/documentation.md", "docs/architecture.md"} <= set(DOCS_PAGES)
