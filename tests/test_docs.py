"""Mechanical checks that keep the documentation in step with the code.

Every backticked path names something that exists, every dotted symbol
resolves by import, every ``invective run`` or ``invective sweep`` line in
a fenced code block parses against the real parser, and every ``pytest
--mutate`` option is one the plugin registers. The header shape, the
90-character prose wrap, and the absence of line-number citations and dash
asides are enforced as well.
"""

import importlib
import os
import re
import sys

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOC_FILES = ["README.md", "CLAUDE.md"]

_DOCS_DIR = os.path.join(ROOT, "docs")
_PLANS_DIR = os.path.join(_DOCS_DIR, "plans")


def _collect_md():
    """Every markdown file that documentation rules apply to."""
    paths = []
    for name in DOC_FILES:
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            paths.append(p)
    for dirpath in (_DOCS_DIR, _PLANS_DIR):
        if not os.path.isdir(dirpath):
            continue
        for entry in sorted(os.listdir(dirpath)):
            if entry.endswith(".md"):
                paths.append(os.path.join(dirpath, entry))
    return paths


ALL_MD = _collect_md()


def _relpath(path):
    return os.path.relpath(path, ROOT)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _lines(path):
    return _read(path).splitlines()


# ---------------------------------------------------------------------------
# Fenced code blocks
# ---------------------------------------------------------------------------

_FENCE_OPEN = re.compile(r"^```(\w*)")
_FENCE_CLOSE = re.compile(r"^```\s*$")


def _fenced_blocks(lines):
    """Yield (info_string, [lines], start_lineno) for each fenced block."""
    inside = False
    info = ""
    block = []
    start = 0
    for i, line in enumerate(lines, 1):
        if inside:
            if _FENCE_CLOSE.match(line):
                yield info, block, start
                inside = False
                block = []
            else:
                block.append(line)
        else:
            m = _FENCE_OPEN.match(line)
            if m:
                inside = True
                info = m.group(1)
                block = []
                start = i


# ---------------------------------------------------------------------------
# 1. Backticked paths resolve
# ---------------------------------------------------------------------------

# Match `some/path` -- must have at least one slash to be a filesystem
# path.  Bare filenames like `conftest.py` are too ambiguous (they name a
# kind of file, not a path in the repo).  Dotted symbols like
# `mutate.OPERATORS` are checked by a separate test.
_BACKTICK_PATH = re.compile(
    r"`([A-Za-z0-9_./-]+/[A-Za-z0-9_./*-]+)`"
)

# Patterns that look like paths but are not filesystem references.
_NOT_A_PATH = re.compile(
    r"^(https?://|git\+https://|v\d+\.\d+|"   # URLs, version tags
    r"test_\w+\.py::\w+|"                       # pytest node ids
    r"sys\.|os\.|"                              # stdlib dotted names
    r"\d)"                                      # starts with a digit
)


def _is_real_path(token):
    """Whether *token* plausibly names a file or directory in the repo."""
    if _NOT_A_PATH.match(token):
        return False
    # Skip tokens that are clearly example paths (src/pkg/...).
    if "src/pkg/" in token:
        return False
    return True


@pytest.mark.parametrize("path", ALL_MD, ids=_relpath)
def test_backticked_paths_exist(path):
    """Every backticked path in documentation names something that exists."""
    missing = []
    for i, line in enumerate(_lines(path), 1):
        # Skip lines inside fenced code blocks (examples may use
        # hypothetical paths).
        pass  # handled below

    # Re-scan, skipping fenced blocks.
    lines = _lines(path)
    in_fence = False
    for i, line in enumerate(lines, 1):
        if _FENCE_CLOSE.match(line) and in_fence:
            in_fence = False
            continue
        if _FENCE_OPEN.match(line):
            in_fence = True
            continue
        if in_fence:
            continue
        for m in _BACKTICK_PATH.finditer(line):
            token = m.group(1)
            if not _is_real_path(token):
                continue
            # Allow patterns with wildcards.
            if "*" in token or "?" in token:
                continue
            candidate = os.path.join(ROOT, token)
            if not (os.path.exists(candidate)
                    or os.path.exists(candidate.rstrip("/"))):
                missing.append((i, token))
    if missing:
        report = "\n".join("  line %d: %s" % (n, t) for n, t in missing)
        pytest.fail("paths that do not exist in %s:\n%s" % (_relpath(path),
                                                            report))


# ---------------------------------------------------------------------------
# 2. Dotted symbols resolve by import
# ---------------------------------------------------------------------------

# Match `module.SYMBOL` patterns -- a module name from invective's
# packages, a dot, and an uppercase-initial or all-caps name.
_DOTTED = re.compile(
    r"`((?:invective\.)?(?:mutate|sweep|tree|accept|config|errors|"
    r"pytest_invective|__main__))"
    r"\.([A-Za-z_]\w*)`"
)

# File extensions that look like attributes but are not.
_NOT_ATTR = {"py", "md", "cfg", "ini", "toml", "yml", "yaml", "txt",
             "json", "lock", "sh"}


@pytest.mark.parametrize("path", ALL_MD, ids=_relpath)
def test_dotted_symbols_resolve(path):
    """Every `module.SYMBOL` in documentation resolves by import."""
    unresolved = []
    seen = set()
    for i, line in enumerate(_lines(path), 1):
        for m in _DOTTED.finditer(line):
            mod_name = m.group(1)
            attr = m.group(2)
            key = (mod_name, attr)
            if key in seen:
                continue
            seen.add(key)
            # Skip file extensions misread as attributes.
            if attr in _NOT_ATTR:
                continue
            # Normalise: bare module names become their full path.
            if not mod_name.startswith(("invective.", "pytest_invective")):
                full = "invective." + mod_name
            else:
                full = mod_name
            try:
                mod = importlib.import_module(full)
            except ImportError:
                unresolved.append((i, "%s.%s" % (mod_name, attr),
                                   "module not found"))
                continue
            if not hasattr(mod, attr):
                unresolved.append((i, "%s.%s" % (mod_name, attr),
                                   "attribute not found"))
    if unresolved:
        report = "\n".join("  line %d: %s (%s)" % (n, s, r)
                           for n, s, r in unresolved)
        pytest.fail("symbols that do not resolve in %s:\n%s"
                    % (_relpath(path), report))


# ---------------------------------------------------------------------------
# 3. invective run / invective sweep commands parse
# ---------------------------------------------------------------------------

def _invective_run_parser():
    from invective.mutate import parser
    return parser()


def _invective_sweep_parser():
    from invective.sweep import parser
    return parser()


@pytest.mark.parametrize("path", ALL_MD, ids=_relpath)
def test_invective_commands_parse(path):
    """Every invective command in a fenced code block parses."""
    errors = []
    lines = _lines(path)
    for info, block, start in _fenced_blocks(lines):
        if info not in ("console", ""):
            continue
        for j, bline in enumerate(block):
            # Strip a leading $ prompt.
            cmd = bline.lstrip()
            if cmd.startswith("$ "):
                cmd = cmd[2:]
            if cmd.startswith("invective run "):
                argv = cmd.split()[2:]  # drop "invective run"
                ap = _invective_run_parser()
                try:
                    ap.parse_args(argv)
                except SystemExit:
                    errors.append((start + j + 1, bline.strip()))
            elif cmd.startswith("invective sweep "):
                argv = cmd.split()[2:]
                ap = _invective_sweep_parser()
                try:
                    ap.parse_args(argv)
                except SystemExit:
                    errors.append((start + j + 1, bline.strip()))
    if errors:
        report = "\n".join("  line %d: %s" % (n, c) for n, c in errors)
        pytest.fail("commands that do not parse in %s:\n%s"
                    % (_relpath(path), report))


# ---------------------------------------------------------------------------
# 4. pytest --mutate options are registered
# ---------------------------------------------------------------------------

def _plugin_options():
    """The long option names the plugin registers."""
    import pytest_invective
    # Walk pytest_addoption to find the option names it registers.
    # We parse the source rather than running pytest, which is simpler.
    src = os.path.join(ROOT, "src", "pytest_invective", "__init__.py")
    text = _read(src)
    return set(re.findall(r'"(--mutate\S*)"', text))


@pytest.mark.parametrize("path", ALL_MD, ids=_relpath)
def test_pytest_mutate_options_registered(path):
    """Every ``--mutate*`` option the docs mention is one the plugin has."""
    registered = _plugin_options()
    unknown = []
    for i, line in enumerate(_lines(path), 1):
        for m in re.finditer(r"`(--mutate(?:-\w+)*)`", line):
            opt = m.group(1)
            if opt not in registered:
                unknown.append((i, opt))
    if unknown:
        report = "\n".join("  line %d: %s" % (n, o) for n, o in unknown)
        pytest.fail("unregistered --mutate options in %s:\n%s"
                    % (_relpath(path), report))


# ---------------------------------------------------------------------------
# 5. Header shape: line 1 is # <subject>, line 2 blank, lines 3-4 summary
# ---------------------------------------------------------------------------

# Only docs/ files and plans/ files get the header check (README.md and
# CLAUDE.md have their own shape).
_HEADER_FILES = [p for p in ALL_MD
                 if os.sep + "docs" + os.sep in p
                 or p.endswith(os.sep + "docs" + os.sep[:-1])]


@pytest.mark.parametrize("path", _HEADER_FILES, ids=_relpath)
def test_header_shape(path):
    """docs/ files follow the header convention: # heading, blank, summary."""
    lines = _lines(path)
    errors = []
    if not lines:
        errors.append("file is empty")
    elif not lines[0].startswith("# "):
        errors.append("line 1 is not a # heading: %r" % lines[0])
    if len(lines) >= 2 and lines[1].strip():
        errors.append("line 2 is not blank: %r" % lines[1])
    # Lines 3-4 should be the summary (non-empty, not a heading).
    if len(lines) >= 3:
        if not lines[2].strip():
            errors.append("line 3 (summary) is blank")
        elif lines[2].startswith("#"):
            errors.append("line 3 is a heading, not a summary")
    if errors:
        pytest.fail("header in %s:\n  %s" % (_relpath(path),
                                             "\n  ".join(errors)))


# ---------------------------------------------------------------------------
# 6. Prose wraps at 90 characters
# ---------------------------------------------------------------------------

# Lines that are allowed to exceed 90 characters.
def _is_exempt(line):
    """Lines exempt from the 90-character wrap."""
    stripped = line.strip()
    # Fenced code markers, table rows, URLs, headings, blockquotes with
    # findings, and HTML comments are exempt.
    if stripped.startswith(("```", "|", "http://", "https://", "<!--")):
        return True
    # Indented content (4+ spaces or tab) is code.
    if line.startswith("    ") or line.startswith("\t"):
        return True
    # Lines that are mostly a URL (backticked or bare).
    if re.search(r"https?://\S{40,}", stripped):
        return True
    return False


@pytest.mark.parametrize("path", ALL_MD, ids=_relpath)
def test_prose_wrap_90(path):
    """Prose lines in documentation wrap at 90 characters."""
    long_lines = []
    lines = _lines(path)
    in_fence = False
    for i, line in enumerate(lines, 1):
        if _FENCE_CLOSE.match(line) and in_fence:
            in_fence = False
            continue
        if _FENCE_OPEN.match(line):
            in_fence = True
            continue
        if in_fence:
            continue
        if len(line) > 90 and not _is_exempt(line):
            long_lines.append((i, len(line)))
    if long_lines:
        report = "\n".join("  line %d: %d chars" % (n, c)
                           for n, c in long_lines)
        pytest.fail("lines over 90 chars in %s:\n%s"
                    % (_relpath(path), report))


# ---------------------------------------------------------------------------
# 7. No line-number citations (":42", "line 42")
# ---------------------------------------------------------------------------

# "line 42" with a number above 4 (small numbers describe file structure,
# not code locations) or ":42" at the end of a .py path.
_LINE_CITE = re.compile(
    r"(?:line\s+(?:[5-9]|\d{2,4})\b"  # "line N" with N >= 5
    r"|\.py:\d{1,4}\b)",               # "module.py:42"
    re.IGNORECASE,
)

# Patterns that look like line citations but are not.
_LINE_CITE_FALSE = re.compile(
    r"(?:"
    r"3\.1[1-4]|"              # Python versions like 3.11, 3.14
    r"8\.\d|9\.\d|"           # pytest versions
    r"v\d+\.\d+\.\d+|"        # version strings
    r"exit\s+\d"               # exit codes
    r")"
)


@pytest.mark.parametrize("path", ALL_MD, ids=_relpath)
def test_no_line_number_citations(path):
    """Documentation does not cite line numbers (the code changes)."""
    citations = []
    lines = _lines(path)
    in_fence = False
    for i, line in enumerate(lines, 1):
        if _FENCE_CLOSE.match(line) and in_fence:
            in_fence = False
            continue
        if _FENCE_OPEN.match(line):
            in_fence = True
            continue
        if in_fence:
            continue
        for m in _LINE_CITE.finditer(line):
            # Skip false positives.
            context = line[max(0, m.start() - 10):m.end() + 10]
            if _LINE_CITE_FALSE.search(context):
                continue
            # Skip things like "gate.py:4" in example output.
            before = line[:m.start()]
            if before.rstrip().endswith(".py"):
                continue
            citations.append((i, m.group()))
    if citations:
        report = "\n".join("  line %d: %r" % (n, c)
                           for n, c in citations)
        pytest.fail("line-number citations in %s:\n%s"
                    % (_relpath(path), report))


# ---------------------------------------------------------------------------
# 8. No dash asides (use parentheses)
# ---------------------------------------------------------------------------

# An em dash or spaced en dash used as an aside, not in a list or heading.
_DASH_ASIDE = re.compile(r"\s--\s|\s-\s(?!-)")


# CLAUDE.md predates the dash-aside rule and cannot be edited freely.
_DASH_FILES = [p for p in ALL_MD if not p.endswith("CLAUDE.md")]


@pytest.mark.parametrize("path", _DASH_FILES, ids=_relpath)
def test_no_dash_asides(path):
    """Asides use parentheses, not dashes (docs/documentation.md rule)."""
    violations = []
    lines = _lines(path)
    in_fence = False
    for i, line in enumerate(lines, 1):
        if _FENCE_CLOSE.match(line) and in_fence:
            in_fence = False
            continue
        if _FENCE_OPEN.match(line):
            in_fence = True
            continue
        if in_fence:
            continue
        stripped = line.lstrip()
        # List items starting with - are not asides.
        if stripped.startswith("- ") or stripped.startswith("* "):
            # But a dash aside within a list item still counts.
            # Check the part after the list marker.
            rest = stripped[2:]
        else:
            rest = line
        # Skip headings.
        if stripped.startswith("#"):
            continue
        for m in _DASH_ASIDE.finditer(rest):
            # " -- " is the pattern; skip if it's a CLI flag like --target.
            after = rest[m.end():]
            if after and after[0] == "-":
                continue
            violations.append((i, line.strip()[:60]))
    if violations:
        report = "\n".join("  line %d: %s" % (n, t)
                           for n, t in violations)
        pytest.fail("dash asides in %s:\n%s" % (_relpath(path), report))


# ---------------------------------------------------------------------------
# 9. The forwarded options listed in README match the plugin
# ---------------------------------------------------------------------------

def test_forwarded_options_match_plugin():
    """The forwarded options README lists are the ones the plugin forwards."""
    readme = _read(os.path.join(ROOT, "README.md"))
    # The README line: "These options reach every mutant's run as you gave
    # them: `-p`, `-o`, ..."
    m = re.search(r"These options reach.*?:\s*(.*?)\.(?:\s|\n)", readme,
                  re.DOTALL)
    assert m, "forwarded-options sentence not found in README"
    text = m.group(1)
    doc_opts = set(re.findall(r"`(-\w+|--[\w-]+)`", text))

    # The plugin's _forwarded function handles these flags.  Extract the
    # function body and find the option strings it builds.
    plugin_src = _read(os.path.join(ROOT, "src", "pytest_invective",
                                    "__init__.py"))
    # Find the _forwarded function body.
    fn_match = re.search(r"def _forwarded\(.*?\n(.*?)(?=\ndef |\Z)",
                         plugin_src, re.DOTALL)
    assert fn_match, "_forwarded function not found in plugin"
    fn_body = fn_match.group(1)

    code_opts = set()
    # Short flags passed with forwarded += ["-X", value].
    for m2 in re.finditer(r'forwarded\s*\+=\s*\["-(\w)"', fn_body):
        code_opts.add("-" + m2.group(1))
    # Long flags: "--flag=..." or ("--flag", ...).
    for m2 in re.finditer(r'"(--[\w-]+)', fn_body):
        code_opts.add(m2.group(1))

    missing_from_docs = code_opts - doc_opts
    extra_in_docs = doc_opts - code_opts
    problems = []
    if missing_from_docs:
        problems.append("plugin forwards but README omits: %s"
                        % ", ".join(sorted(missing_from_docs)))
    if extra_in_docs:
        problems.append("README lists but plugin does not forward: %s"
                        % ", ".join(sorted(extra_in_docs)))
    if problems:
        pytest.fail("\n".join(problems))


# ---------------------------------------------------------------------------
# 10. The edits table in README matches OPERATORS
# ---------------------------------------------------------------------------

def test_edits_table_matches_operators():
    """The edits table in README lists exactly the keys in OPERATORS."""
    from invective.mutate import OPERATORS

    readme = _read(os.path.join(ROOT, "README.md"))
    # Find the table rows: | `NAME` | ... |
    table_names = set(re.findall(r"\|\s*`(\w+)`\s*\|", readme))
    code_names = set(OPERATORS)

    missing = code_names - table_names
    extra = table_names - code_names
    problems = []
    if missing:
        problems.append("OPERATORS has but table omits: %s"
                        % ", ".join(sorted(missing)))
    if extra:
        problems.append("table has but OPERATORS omits: %s"
                        % ", ".join(sorted(extra)))
    if problems:
        pytest.fail("\n".join(problems))
