"""The sweep: which modules it mutates, and which tests it runs against each."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time

import pytest

from invective import mutate, sweep

from conftest import (FILES, MONOREPO, SLOW_TEST, SRC, WORKSPACE, monorepo,
                      no_pytest_settings_above, stop_group, wait_for,
                      write_tree)

SOURCES = ["pkg", "loose"]
TESTS_DIR = "pkg/tests"


def _p(rel):
    return os.path.join(*rel.split("/"))


def test_the_suite_s_own_files_are_never_mutation_targets(tree):
    """`conftest.py` and `helpers.py` do not start `test_`, and are not modules.

    A harness file is imported by nearly every test, so mutating one re-runs
    most of the suite per mutant. The directory beside the suite whose name
    merely starts the same way is still source.
    """
    found = set(sweep.modules(tree, SOURCES, TESTS_DIR))

    assert found == {_p(m) for m in (
        "pkg/__init__.py", "pkg/gate.py", "pkg/idle.py",
        "pkg/sub/__init__.py", "pkg/sub/deep.py",
        "pkg/tests_extra/aid.py", "loose/tool.py")}


@pytest.mark.parametrize("module, name", [
    ("pkg/gate.py", "pkg.gate"),
    ("pkg/sub/deep.py", "pkg.sub.deep"),
    ("pkg/sub/__init__.py", "pkg.sub"),
    ("pkg/__init__.py", "pkg"),
    ("loose/tool.py", None),
])
def test_a_module_is_named_as_its_tests_import_it(tree, module, name):
    assert sweep.import_name(tree, _p(module), SOURCES) == name


def test_a_package_under_src_is_named_from_src(tmp_path):
    """The import root is the first directory above the package that is not one."""
    write_tree(str(tmp_path), {"src/pkg/__init__.py": "",
                               "src/pkg/a/b.py": "x = 1\n"})

    # `a` holds no `__init__.py` and is still part of the dotted name: the
    # package is the directory `--src` named, not each directory below it.
    assert sweep.import_name(str(tmp_path), _p("src/pkg/a/b.py"),
                             ["src/pkg"]) == "pkg.a.b"


def test_a_package_below_a_source_directory_that_is_not_one_is_named_from_its_own_top(
        tmp_path):
    """`--src src` over `src/pkg/` still finds the tests that import `pkg.gate`."""
    root = str(tmp_path)
    write_tree(root, {"src/pkg/__init__.py": "",
                      "src/pkg/gate.py": "x = 1\n",
                      "tests/sub/test_x.py": "from pkg import gate\n",
                      "scripts/tool.py": "x = 1\n"})

    assert sweep.import_name(root, _p("src/pkg/gate.py"), ["src"]) == "pkg.gate"
    assert sweep.covering(root, _p("src/pkg/gate.py"), ["src"], "tests") == [
        _p("tests/sub/test_x.py")]
    # A directory of loose files stays loose.
    assert sweep.import_name(root, _p("scripts/tool.py"), ["scripts"]) is None


@pytest.mark.parametrize("line", [
    "import pkg.gate",
    "from pkg.gate import admit",
    "from pkg import gate",
    "from pkg import idle, gate",
    "x = pkg.gate.admit(30, False)",
])
def test_a_test_file_covers_the_module_it_imports(tree, line):
    write_tree(tree, {"pkg/tests/test_other.py": line + "\n"})

    assert _p("pkg/tests/test_other.py") in sweep.covering(
        tree, _p("pkg/gate.py"), SOURCES, TESTS_DIR)


@pytest.mark.parametrize("line", [
    "# the gate is checked elsewhere",
    "name = 'gate'",
    "from pkg import idle",
    "import pkg.gateway",
])
def test_a_name_in_passing_is_not_an_import(tree, line):
    """Matching the bare word would select most of a suite for a common name."""
    write_tree(tree, {"pkg/tests/test_other.py": line + "\n"})

    assert _p("pkg/tests/test_other.py") not in sweep.covering(
        tree, _p("pkg/gate.py"), SOURCES, TESTS_DIR)


def test_a_loose_module_is_covered_by_its_bare_import(tree):
    write_tree(tree, {"pkg/tests/test_other.py": "from tool import tool\n"})

    assert sweep.covering(tree, _p("loose/tool.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_other.py"), _p("pkg/tests/test_tool.py")]


def test_the_file_named_after_a_module_covers_it_without_importing_it(tree):
    write_tree(tree, {"pkg/tests/test_idle.py": "def test_nothing():\n    pass\n"})

    assert sweep.covering(tree, _p("pkg/idle.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_idle.py")]


def test_a_test_file_in_a_subdirectory_covers_the_module_it_imports(tree):
    """A suite grouped into directories (`tests/browser/`) is still the suite.

    The top-level files come first and each subdirectory follows in name
    order, so the map is the same list on every run.
    """
    write_tree(tree, {
        "pkg/tests/sub/test_idle_deep.py": "from pkg import idle\n",
        "pkg/tests/sub/more/test_idle_deeper.py": "from pkg import idle\n",
        "pkg/tests/aaa/test_idle_first.py": "from pkg import idle\n",
        "pkg/tests/test_idle_top.py": "from pkg import idle\n",
        # Not tests: a harness file and a helper in the subdirectory.
        "pkg/tests/sub/conftest.py": "from pkg import idle\n",
        "pkg/tests/sub/helpers.py": "from pkg import idle\n",
    })

    assert sweep.covering(tree, _p("pkg/idle.py"), SOURCES, TESTS_DIR) == [
        _p(f) for f in ("pkg/tests/test_idle_top.py",
                        "pkg/tests/aaa/test_idle_first.py",
                        "pkg/tests/sub/test_idle_deep.py",
                        "pkg/tests/sub/more/test_idle_deeper.py")]


def test_the_file_named_after_a_module_covers_it_from_the_mirror_directory(tree):
    write_tree(tree, {"pkg/tests/sub/test_deep.py": "def test_nothing():\n    pass\n"})

    assert sweep.covering(tree, _p("pkg/sub/deep.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_deep_things.py"), _p("pkg/tests/sub/test_deep.py")]


@pytest.mark.parametrize("sources", [["src"], ["src/acme"]])
def test_a_package_below_a_namespace_directory_is_covered_by_the_tests_that_import_it(
        tmp_path, sources):
    """`src/acme/` has no `__init__.py`, so `import_name` names the module
    from the package's own top (`gate.core`) while the tests import it under
    the namespace (`acme.gate`)."""
    root = str(tmp_path)
    write_tree(root, {
        "src/acme/gate/__init__.py": "",
        "src/acme/gate/core.py": "x = 1\n",
        "tests/test_attr.py": "import acme\nacme.gate.core.x\n",
        "tests/test_dotted.py": "import acme.gate.core\n",
        "tests/test_from.py": "from acme.gate.core import x\n",
        "tests/test_rules.py": "from acme.gate import core\n",
        "tests/unit/test_rules_deep.py": "from acme.gate import core\n",
        "tests/unit/test_other.py": "from email.gate import core\n"
                                    "from other.gate.core import x\n"
                                    "import other.gate.core\n"
                                    "other.gate.core.x\n"
                                    # `src` alone is no prefix: only
                                    # `acme.` or `src.acme.` is.
                                    "from src.gate import core\n",
    })

    assert sorted(sweep.covering(root, _p("src/acme/gate/core.py"), sources,
                                 "tests")) == [
        _p("tests/test_attr.py"), _p("tests/test_dotted.py"), _p("tests/test_from.py"),
        _p("tests/test_rules.py"), _p("tests/unit/test_rules_deep.py")]


def test_a_top_level_package_is_not_covered_by_a_library_module_ending_in_its_name(
        tmp_path):
    """`email.utils`, `django.utils.text`, `unittest.mock` and `os.path` end
    in the name of a package of the project's own; a test using them uses
    nothing of the project's but `app`. `from email import utils` binds the
    library's module to the package's name, and `from . import utils` a
    helper of the tests' own; neither loads the package, and `utils.` after
    either is no more the package's than the bare stem of a loose module."""
    root = str(tmp_path)
    write_tree(root, {
        "src/utils/__init__.py": "",
        "src/utils/text.py": "x = 1\n",
        "src/mock/__init__.py": "",
        "src/path/__init__.py": "",
        "src/app/__init__.py": "",
        "src/app/core.py": "x = 1\n",
        "tests/test_app.py": "from email.utils import parseaddr\n"
                             "from django.utils.text import slugify\n"
                             "import unittest.mock\n"
                             "import os.path\n"
                             "from app import core\n"
                             "HERE = os.path.dirname(__file__)\n",
        "tests/test_mail.py": "from email import utils\n",
        "tests/unit/test_mail.py": "from email import utils\n"
                                   "utils.parseaddr('a')\n",
        "tests/api/__init__.py": "",
        "tests/api/utils.py": "x = 1\n",
        "tests/api/test_api.py": "from . import utils\n",
    })

    for module in ("src/utils/__init__.py", "src/utils/text.py",
                   "src/mock/__init__.py", "src/path/__init__.py"):
        assert sweep.covering(root, _p(module), ["src"], "tests") == [], module
    assert sweep.covering(root, _p("src/app/core.py"), ["src"], "tests") == [
        _p("tests/test_app.py")]
    # An import statement that names the package among others still loads it.
    write_tree(root, {"tests/unit/test_u.py": "import os, utils\n"})
    assert sweep.covering(root, _p("src/utils/__init__.py"), ["src"],
                          "tests") == [_p("tests/unit/test_u.py")]
    write_tree(root, {"tests/unit/test_u.py": "import os as o, utils\n"})
    assert sweep.covering(root, _p("src/utils/__init__.py"), ["src"],
                          "tests") == [_p("tests/unit/test_u.py")]


def test_a_from_statement_naming_a_submodule_covers_the_package_anywhere(
        tmp_path):
    """`from pkg.gate import x` loads `pkg/__init__.py` as `import pkg.gate`
    does, so a test file in any subdirectory of the tests covers it."""
    root = str(tmp_path)
    write_tree(root, {"src/pkg/__init__.py": "x = 1\n",
                      "src/pkg/gate.py": "x = 1\n",
                      "tests/unit/test_from.py": "from pkg.gate import x\n"})

    assert sweep.covering(root, _p("src/pkg/__init__.py"), ["src"],
                          "tests") == [_p("tests/unit/test_from.py")]
    # The statement must start the line and name the package itself.
    for other in ("from email.pkg.x import y", "from .pkg.gate import x",
                  "# from pkg.gate import x", "from django.pkg.gate import x"):
        write_tree(root, {"tests/unit/test_from.py": other + "\n"})
        assert sweep.covering(root, _p("src/pkg/__init__.py"), ["src"],
                              "tests") == [], other


def _top_level_rules(root, module, sources):
    """`covering`'s rules at 5241e5f, which reads only the top of the tests
    directory: the module's dotted name when its source directory is a
    package, else its bare stem, and the patterns matched on that name.
    Returns the name and the patterns."""
    mod = os.path.abspath(os.path.join(root, module))
    home = os.path.dirname(mod)
    for base in sources:
        base = os.path.abspath(os.path.join(root, base))
        if mod.startswith(base + os.sep):
            home = base
            break
    dotted = None
    if os.path.isfile(os.path.join(home, "__init__.py")):
        top = home
        while os.path.isfile(os.path.join(os.path.dirname(top), "__init__.py")):
            top = os.path.dirname(top)
        rel = os.path.relpath(mod, os.path.dirname(top))
        dotted = rel[:-3].replace(os.sep, ".").replace(".__init__", "")
    stem = os.path.basename(module)[:-3]
    if stem in ("__init__", "__main__"):
        stem = os.path.basename(os.path.dirname(module))
    if dotted:
        return dotted, [
            r"\bimport\s+%s\b" % re.escape(dotted),
            r"from\s+%s\s+import" % re.escape(dotted),
            r"from\s+%s\s+import\s+.*\b%s\b"
            % (re.escape(dotted.rsplit(".", 1)[0]), re.escape(stem)),
            r"\b%s\." % re.escape(dotted)]
    return None, [r"^\s*import\s+%s\b" % re.escape(stem),
                  r"^\s*from\s+%s\s+import" % re.escape(stem),
                  r"\b%s\.\w" % re.escape(stem)]


#: Each module, the source directories it is swept under, and the directory
#: under `tests/` that mirrors its own: a top-level package and a module in
#: it, a package below a namespace directory, a loose file, and a package
#: that is its own source directory.
_PROBED = [
    ("src/utils/__init__.py", ["src"], "utils"),
    ("src/utils/text.py", ["src"], "utils"),
    ("src/acme/gate/__init__.py", ["src"], "acme/gate"),
    ("src/acme/gate/core.py", ["src"], "acme/gate"),
    ("src/acme/gate/core.py", ["src/acme"], "gate"),
    ("scripts/config.py", ["scripts"], "."),
    ("app/core.py", ["app"], "."),
]

#: Where a probing test file is written: the top of `tests/`, a directory
#: that mirrors no module, and every module's mirror.
_PLACES = [".", "unit", "utils", "acme/gate", "gate"]

#: What a test file may say. Most of it loads none of the modules probed.
_PROBES = [
    "from email import utils",
    "from email import utils\nutils.parseaddr('a')",
    "from . import utils",
    "from .utils import x",
    "from email.utils import parseaddr",
    "import email.utils",
    "from django.utils.text import slugify",
    "from django import utils\nutils.text.slugify('a')",
    "import os.path\nos.path.join('a')",
    "x = os.utils.text",
    "# import utils",
    "'import utils'",
    "from pkg import utils, text, config, core, gate",
    "from src.gate import core",
    "from other.gate import core",
    "from other.gate.core import x",
    "import other.gate.core\nother.gate.core.x",
    "gate.core.x",
    "import gate",
    "import core\ncore.x",
    "from core import x",
    "text.x",
    "from logging import config\nconfig.dictConfig({})",
    "import config",
    "from config import X",
    "import scripts.config",
    "from app import core",
    "import app.core\napp.core.x",
    "import utils",
    "import os, utils",
    "import utils as u",
    "from utils import f",
    "from utils import text",
    "from utils.text import slugify",
    "import utils.text\nutils.text.x",
    "import acme.gate",
    "import acme.gate.core",
    "import acme\nacme.gate.core.x",
    "from acme.gate import core",
    "from acme.gate.core import x",
    "from src.acme.gate.core import x",
]

#: A test file that loads the module in a way the top-level rules cannot
#: see: they name a package under a source directory that is not one by its
#: bare stem, and read no import statement that lists other modules before
#: it. These count wherever the file is.
_LOADS = {
    ("import utils", "src/utils/__init__.py"),
    ("import os, utils", "src/utils/__init__.py"),
    ("import utils as u", "src/utils/__init__.py"),
    ("from utils import f", "src/utils/__init__.py"),
    ("from utils import text", "src/utils/__init__.py"),
    ("import utils.text\nutils.text.x", "src/utils/__init__.py"),
    ("from utils import text", "src/utils/text.py"),
    ("from utils.text import slugify", "src/utils/text.py"),
    ("import utils.text\nutils.text.x", "src/utils/text.py"),
    ("import gate", "src/acme/gate/__init__.py"),
    ("import acme.gate", "src/acme/gate/__init__.py"),
    ("import acme.gate.core", "src/acme/gate/__init__.py"),
    ("from acme.gate import core", "src/acme/gate/__init__.py"),
    ("import acme.gate.core", "src/acme/gate/core.py"),
    ("import acme\nacme.gate.core.x", "src/acme/gate/core.py"),
    ("from acme.gate import core", "src/acme/gate/core.py"),
    ("from acme.gate.core import x", "src/acme/gate/core.py"),
    ("from src.acme.gate.core import x", "src/acme/gate/core.py"),
    # A `from` statement naming a submodule loads the package as well.
    ("from acme.gate.core import x", "src/acme/gate/__init__.py"),
    ("from src.acme.gate.core import x", "src/acme/gate/__init__.py"),
    ("from utils.text import slugify", "src/utils/__init__.py"),
}

#: The one shape of the dotted attribute rule that matches without loading
#: the module: a library's module bound to the package's top name, followed
#: by an attribute named like the module. The top-level rules match these at
#: the top of `tests/` too, by the module's bare stem.
_ALIKE = {
    ("from django import utils\nutils.text.slugify('a')", "src/utils/text.py"),
    ("gate.core.x", "src/acme/gate/core.py"),
}


def test_no_test_file_covers_a_module_the_top_level_rules_leave_uncovered(
        tmp_path):
    """The walk into subdirectories finds more test files, and finds them by
    the rules that held at the top of the tests directory, applied where they
    held: a dotted name anywhere, a bare stem only at the top and in the
    mirror directory. No file covers a module those rules leave uncovered,
    save one that loads it by a name they cannot see, and the attribute
    shapes in `_ALIKE`."""
    root = str(tmp_path)
    write_tree(root, {m: "x = 1\n" for m, _s, _d in _PROBED})
    write_tree(root, {"src/utils/__init__.py": "", "src/acme/gate/__init__.py": "",
                      "app/__init__.py": ""})
    wider = []
    for probe in _PROBES:
        write_tree(root, {"tests/%s/test_probe.py" % place: probe + "\n"
                          for place in _PLACES})
        for module, sources, mirror in _PROBED:
            if (probe, module) in _LOADS | _ALIKE:
                continue
            found = sweep.covering(root, _p(module), sources, "tests")
            dotted, patterns = _top_level_rules(root, module, sources)
            matched = any(re.search(pat, probe + "\n", re.M) for pat in patterns)
            for place in _PLACES:
                held = matched and (dotted is not None or place in (".", mirror))
                where = os.path.normpath(_p("tests/%s/test_probe.py" % place))
                if where in found and not held:
                    wider.append((probe, module, sources, place))

    assert wider == []


def test_a_package_at_the_project_s_top_is_imported_with_no_prefix(tmp_path):
    """A relative import in the tests names a package of the tests' own:
    `from ..app.core` in `tests/unit/` is `tests.app.core`, never the
    project's `app/core.py`."""
    root = str(tmp_path)
    write_tree(root, {
        "app/__init__.py": "",
        "app/core.py": "x = 1\n",
        "tests/__init__.py": "",
        "tests/unit/__init__.py": "",
        "tests/unit/test_rel.py": "from ..app.core import x\n",
        "tests/unit/test_abs.py": "from app.core import x\n",
    })

    assert sweep.covering(root, _p("app/core.py"), ["."], "tests") == [
        _p("tests/unit/test_abs.py")]


def test_a_file_named_after_a_module_elsewhere_is_another_module_s(tmp_path):
    """`models.py` and `test_models.py` recur in every app of a mirrored
    layout. The file named after a module covers it at the top of the tests
    directory and where the module's own directory is mirrored, and anywhere
    else only by importing it."""
    root = str(tmp_path / "proj")
    write_tree(root, {
        "src/app/__init__.py": "",
        "src/app/api/__init__.py": "",
        "src/app/api/models.py": "X = 1\n",
        "src/app/db/__init__.py": "",
        "src/app/db/models.py": "X = 2\n",
        "tests/api/test_models.py": "from app.api import models\n",
        "tests/zz/test_models.py": "def test_nothing():\n    pass\n",
    })

    assert sweep.covering(root, "src/app/db/models.py", ["src/app"], "tests") == []
    assert sweep.covering(root, "src/app/api/models.py", ["src/app"], "tests") == [
        os.path.join("tests", "api", "test_models.py")]
    write_tree(root, {"tests/api/test_models.py": "def test_nothing():\n    pass\n"})
    assert sweep.covering(root, "src/app/api/models.py", ["src/app"], "tests") == [
        os.path.join("tests", "api", "test_models.py")]


def test_a_loose_module_is_covered_by_its_bare_stem_only_where_its_name_is(
        tmp_path):
    """A loose module's tests import it by its bare stem, and a bare stem in a
    subdirectory is as likely that directory's own helper or an attribute of
    something else. A module in a package is imported by its dotted path,
    which counts anywhere."""
    root = str(tmp_path / "proj")
    write_tree(root, {
        "scripts/config.py": "X = 1\n",
        "tools/helpers.py": "X = 2\n",
        "pkg/__init__.py": "",
        "pkg/app.py": "X = 3\n",
        "tests/web/helpers.py": "X = 4\n",
        "tests/web/test_app.py": "from pkg import app\n\napp.config.debug\n",
        "tests/web/test_page.py": "import helpers\n",
        "tests/test_config.py": "import config\n",
    })
    sources = ["scripts", "tools", "pkg"]

    assert sweep.covering(root, "scripts/config.py", sources, "tests") == [
        os.path.join("tests", "test_config.py")]
    assert sweep.covering(root, "tools/helpers.py", sources, "tests") == []
    assert sweep.covering(root, "pkg/app.py", sources, "tests") == [
        os.path.join("tests", "web", "test_app.py")]


def test_a_file_under_pycache_in_the_tests_directory_is_not_coverage(tree):
    """Bytecode and stale copies are not what the project wrote."""
    write_tree(tree, {"pkg/tests/__pycache__/test_cached.py": "from pkg import idle\n"})

    assert sweep.covering(tree, _p("pkg/idle.py"), SOURCES, TESTS_DIR) == []


def test_a_harness_file_that_imports_a_module_does_not_cover_it(tree):
    """`helpers.py` imports both modules and holds no tests to run."""
    assert sweep.covering(tree, _p("pkg/gate.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_gate.py")]
    assert sweep.covering(tree, _p("loose/tool.py"), SOURCES, TESTS_DIR) == [
        _p("pkg/tests/test_tool.py")]


class _Engine:
    """Stands in for the engine's process, a `Popen`; `git` is still really
    run, through the real one."""

    def __init__(self, returncode=2, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.commands, self.calls = [], []
        self._real = sweep.subprocess.Popen

    def __call__(self, cmd, **kwargs):
        if cmd[0] == "git":
            return self._real(cmd, **kwargs)
        self.commands.append((cmd, kwargs))
        return self

    def communicate(self, timeout=None):
        return self.stdout, self.stderr

    def wait(self, timeout=None):
        self.calls.append("wait")

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")


def test_the_driver_starts_the_engine_beside_it_with_the_stated_defaults(
        repo, monkeypatch):
    """A path to the engine that names nothing fails quietly: every module
    comes back without a score and the footer says none were measured.

    The defaults are the ones the README states.
    """
    engine = _Engine(stderr="refused: no mutation sites in x for RAISE")
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)

    assert sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                       "--modules", _p("pkg/gate.py")]) == 0

    ((cmd, kwargs),) = engine.commands
    assert cmd[0] == sweep.sys.executable
    assert os.path.isfile(cmd[1]) and os.path.basename(cmd[1]) == "mutate.py"
    assert cmd[2:8] == ["--target", _p("pkg/gate.py"), "--tests",
                        _p("pkg/tests/test_gate.py"), "--only", "RAISE"]
    assert cmd[8:10] == ["--limit", "25"]
    # The same directory, not the same text: git on Windows spells the
    # repository's path with forward slashes.
    assert os.path.samefile(kwargs["cwd"], repo)


def test_modules_with_nothing_after_it_sweeps_no_module(repo, monkeypatch,
                                                         capsys):
    """`--modules $(changed files)` with nothing changed is a request for no
    module, not for every one."""
    engine = _Engine()
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)

    assert sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                       "--modules"]) == 0

    assert engine.commands == []
    assert "0 module(s) measured; 0 with no test file" in capsys.readouterr().out


@pytest.mark.parametrize("said, rc, note", [
    ("refused: no mutation sites in pkg/gate.py for RAISE", 2,
     "no mutation sites"),
    ("refused: the selection is RED on the unmutated tree, so every mutant",
     2, "RED baseline"),
    ("Traceback (most recent call last):\n  ...\n"
     "During handling of the above exception, another exception occurred:\n"
     "ModuleNotFoundError: No module named 'pytest'", 1,
     "DRIVER FAILED rc=1: ModuleNotFoundError: No module named 'pytest'"),
    ("", 3, "DRIVER FAILED rc=3: no output"),
])
def test_a_module_with_no_score_says_why(repo, monkeypatch, tmp_path, said,
                                         rc, note):
    """An engine that could not start must not read as an answer about the
    module. The traceback says "occurred", which holds the letters "red".
    """
    monkeypatch.setattr(sweep.subprocess, "Popen", _Engine(rc, stderr=said))
    out = str(tmp_path / "sweep.json")

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR, "--json", out,
                "--modules", _p("pkg/gate.py")])

    with open(out, encoding="utf-8") as fh:
        (entry,) = json.load(fh)["measured"]
    assert entry["note"] == note and "score" not in entry


def test_a_refusal_the_engine_gave_is_said_whole(
        repo, monkeypatch, capsys, tmp_path):
    """A refusal is the engine's own sentence, and the person who sweeps has
    to read all of it: it carries the path and the advice."""
    sentence = "refused: " + "x" * 200
    monkeypatch.setattr(sweep.subprocess, "Popen", _Engine(
        2, stderr="\n" + sentence + "\n"))
    out = str(tmp_path / "sweep.json")

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR, "--json", out,
                "--modules", _p("pkg/gate.py")])

    with open(out, encoding="utf-8") as fh:
        (entry,) = json.load(fh)["measured"]
    assert entry["note"] == "DRIVER FAILED rc=2: " + sentence
    assert sentence in capsys.readouterr().out


@pytest.mark.parametrize("fixed", [None, "pythonpath"])
def test_a_module_the_tests_import_from_outside_the_copy_fails_the_sweep(
        tmp_path, monkeypatch, capsys, fixed):
    """The tests load the project's own file, as an editable install of a
    `src` layout has them do, so no mutant can reach them: the sweep exits
    2, as `invective run` does on the same tree, and a gate on it does not
    read green with nothing measured. The engine's refusal is still its
    module's row. With pytest's `pythonpath` setting naming `src`, the tests
    load the copy's file and the module is measured. Nothing is stubbed: a
    `PYTHONPATH` entry stands in for the editable install's `.pth` line."""
    project = os.path.realpath(tmp_path / "project")
    write_tree(project, {
        "pyproject.toml": "[project]\nname = 'p'\n[tool.pytest.ini_options]\n"
                          + ("pythonpath = ['src']\n" if fixed else ""),
        "src/pkg/__init__.py": "",
        "src/pkg/gate.py": FILES["pkg/gate.py"],
        "tests/test_gate.py": FILES["pkg/tests/test_gate.py"],
    })
    monkeypatch.chdir(project)
    monkeypatch.setenv("PYTHONPATH",
                       os.pathsep.join([SRC, os.path.join(project, "src")]))
    gate = os.path.join("src", "pkg", "gate.py")

    rc = sweep.main(["--src", os.path.join("src", "pkg"), "--tests-dir",
                     "tests", "--modules", gate])

    out, err = capsys.readouterr()
    if fixed:
        assert rc == 0
        assert "1/1" in out
        return
    assert rc == 2
    assert "DRIVER FAILED rc=2: refused: " in out
    assert "was imported from" in out
    (said,) = [ln for ln in err.splitlines() if ln.startswith("refused:")]
    assert said.endswith(": " + gate)


def test_a_real_sweep_separates_unmeasured_from_killed_and_survived(
        repo, tmp_path, capsys):
    """The whole path with nothing stubbed: the driver starts the engine.

    `idle.py` is imported by no test, and it must come back as unmeasured --
    a module with no covering test is not a module with a perfect score.
    """
    out = str(tmp_path / "sweep.json")

    assert sweep.main(["--src", *SOURCES, "--tests-dir", TESTS_DIR,
                       "--json", out]) == 0

    with open(out, encoding="utf-8") as fh:
        report = json.load(fh)
    assert report["unmeasured"] == [_p("pkg/idle.py"),
                                    _p("pkg/tests_extra/aid.py")]
    by = {r["module"]: r for r in report["measured"]}
    assert set(by) == {_p(m) for m in (
        "pkg/__init__.py", "pkg/gate.py", "pkg/sub/__init__.py",
        "pkg/sub/deep.py", "loose/tool.py")}

    gate = by[_p("pkg/gate.py")]
    assert (gate["killed"], gate["mutants"], gate["survivors"]) == (1, 1, [])
    assert gate["kills"][0]["killer"] == (
        "pkg/tests/test_gate.py::test_a_minor_is_refused")
    assert by[_p("pkg/sub/deep.py")]["score"] == 100.0

    tool = by[_p("loose/tool.py")]
    assert (tool["killed"], tool["mutants"]) == (0, 1)
    assert "SURVIVED" in tool["survivors"][0] and ":3" in tool["survivors"][0]

    # An empty `__init__.py` has tests but nothing to break, which is an
    # answer about the module and not a failure of the driver.
    assert by[_p("pkg/__init__.py")]["note"] == "no mutation sites"
    said = capsys.readouterr().out
    assert "3 module(s) measured; 2 with no test file importing them" in said


def test_the_sweep_s_json_carries_the_engine_s_own_report(repo, tmp_path):
    """A consumer that reads the sweep's file gets the survivors, accepted
    mutants and stale acceptances as the engine reports them, and not the
    printed line of each, which cannot be taken apart again.

    `tool.py` is given one of each: a survivor nothing accepts (the `<`),
    an accepted one (the `raise`), and an acceptance of a mutant its line
    does not have.
    """
    write_tree(repo, {"loose/tool.py": (
        "def tool(n):\n"
        "    if n < 0:\n"
        "        raise ValueError('negative')  # invective: accept[equivalent]"
        " no test asks\n"
        "    return n  # invective: accept[equivalent: 1 -> 3] no such mutant\n"
    )})
    out = str(tmp_path / "sweep.json")

    assert sweep.main(["--src", "loose", "--tests-dir", TESTS_DIR,
                       "--only", "RAISE,CMP", "--json", out]) == 0

    with open(out, encoding="utf-8") as fh:
        (entry,) = json.load(fh)["measured"]
    tool = _p("loose/tool.py")
    direct = mutate.mutate(repo, os.path.join(repo, tool), entry["tests"],
                           ["RAISE", "CMP"], 25, say=lambda line: None)
    # Each list is there to be non-empty: equal empty lists would pass for a
    # report that carried nothing.
    assert direct["survivors"] and direct["accepted"] and direct["stale"]
    got = entry["report"]
    assert got["target"] == entry["module"] == tool
    for kind in ("survivors", "accepted", "stale"):
        assert got[kind] == direct[kind]


@pytest.mark.parametrize("argv", [
    ["--src", "pkgg", "--tests-dir", TESTS_DIR],
    ["--src", "pkg", "--tests-dir", "pkg/test"],
])
def test_a_directory_that_is_not_there_is_refused(repo, capsys, argv):
    """A mistyped path walks as empty, and "0 module(s) measured" reads as a
    result."""
    assert sweep.main(argv) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_a_path_outside_the_project_is_refused(repo, capsys):
    """`..` from the top is a directory, and walked it would sweep whatever
    is above the project."""
    assert sweep.main(["--src", "..", "--tests-dir", TESTS_DIR]) == 2
    assert "is outside the project at" in capsys.readouterr().err


def test_a_report_the_engine_wrote_but_the_driver_cannot_read_is_said(
        repo, monkeypatch, capsys, tmp_path):
    """The score still comes from the printed summary; what only the report
    holds is unknown, not none."""
    monkeypatch.setattr(sweep.subprocess, "Popen", _Engine(
        0, stdout="1/1 killed (100.0%), 0 survived\n"))
    out = str(tmp_path / "sweep.json")

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR, "--json", out,
                "--modules", _p("pkg/gate.py")])

    with open(out, encoding="utf-8") as fh:
        (entry,) = json.load(fh)["measured"]
    assert (entry["score"], entry["kills"], entry["broken"]) == (100.0, None, None)
    assert entry["report"] is None
    assert "(could not read %s's own report)" % _p("pkg/gate.py") in (
        capsys.readouterr().out)


@pytest.mark.parametrize("argv, missing", [
    (["--tests-dir", TESTS_DIR], "--src"),
    (["--src", "pkg"], "--tests-dir"),
])
def test_the_command_says_which_argument_is_missing(capsys, argv, missing):
    with pytest.raises(SystemExit) as stopped:
        sweep.main(argv)
    assert stopped.value.code == 2
    assert missing in capsys.readouterr().err


def test_the_sweep_command_line_is_one_a_test_can_parse():
    """`parser()` is `main`'s own parser, built apart from it so a check of
    the documented commands can parse them without running anything."""
    args = sweep.parser().parse_args(
        ["--src", "pkg", "loose", "--tests-dir", TESTS_DIR,
         "--modules", "pkg/gate.py", "loose/idle.py"])

    assert (args.src, args.tests_dir, args.modules) == (
        ["pkg", "loose"], TESTS_DIR, ["pkg/gate.py", "loose/idle.py"])


def test_the_help_says_what_modules_does(capsys):
    with pytest.raises(SystemExit) as stopped:
        sweep.main(["--help"])
    assert stopped.value.code == 0
    # argparse wraps the help to the terminal's width.
    out = " ".join(capsys.readouterr().out.split())
    assert "--modules" in out
    assert "sweep only these module files" in out
    assert "instead of discovering them under --src" in out


def test_a_module_that_breaks_the_project_s_rules_fails_the_sweep(
        repo, monkeypatch, capsys):
    monkeypatch.setattr(sweep.subprocess, "Popen", _Engine(
        1, stdout=("1/2 killed (50.0%), 1 survived\n"
                   "fails:     1 survivor(s) that no comment accepts\n")))

    assert sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                       "--modules", _p("pkg/gate.py")]) == 1
    said = capsys.readouterr().out
    assert "fails: 1 survivor(s) that no comment accepts" in said
    assert "1 module(s) break the project's rules: %s" % _p("pkg/gate.py") in said


def test_a_ref_given_to_the_sweep_reaches_the_engine(repo, monkeypatch):
    engine = _Engine(0, stdout="1/1 killed (100.0%), 0 survived\n")
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR, "--ref", "main",
                "--modules", _p("pkg/gate.py")])

    ((cmd, _kwargs),) = engine.commands
    assert cmd[cmd.index("--ref") + 1] == "main"


def test_a_terminated_sweep_terminates_the_engine_it_started(repo, monkeypatch):
    """`subprocess.run` kills its child outright on any exception, with the
    engine's copy stranded. The engine is told to stop instead and waited
    for, and the sweep then dies by the signal, as `run` does."""
    engine = _Engine()

    def communicate(timeout=None):
        raise mutate._Terminated(signal.SIGTERM)

    engine.communicate = communicate
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)
    died = []
    monkeypatch.setattr(sweep, "_exit_by", lambda exc: died.append(exc.signum))

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                "--modules", _p("pkg/gate.py")])

    assert engine.calls == ["terminate", "wait"]
    assert died == [signal.SIGTERM]


def test_a_sigterm_between_engines_still_ends_the_sweep_by_the_signal(
        repo, monkeypatch, capsys):
    """Finding the next module's tests takes seconds on a large suite, and
    no engine is running then. A SIGTERM there still goes through the
    sweep's exit by the signal, which writes out the rows printed so far:
    with the default action they would die in the buffer of a file or a
    pipe. The handler is called directly, as the signal would call it, so
    no signal reaches the process running the test."""
    real = sweep.covering
    calls = []

    def covering(*args):
        calls.append(args)
        if len(calls) == 2:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler), handler
            handler(signal.SIGTERM, None)
        return real(*args)

    monkeypatch.setattr(sweep, "covering", covering)
    engine = _Engine(0, stdout="1/1 killed (100.0%), 0 survived\n")
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)
    died = []
    monkeypatch.setattr(sweep, "_exit_by", lambda exc: died.append(exc.signum))

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                "--modules", _p("pkg/gate.py"), _p("pkg/sub/deep.py")])

    assert died == [signal.SIGTERM]
    assert len(engine.commands) == 1
    said = capsys.readouterr().out
    assert re.search(r"^%s +1/1 " % re.escape(_p("pkg/gate.py")), said, re.M)
    assert _p("pkg/sub/deep.py") not in said


@pytest.mark.skipif(os.name == "nt", reason="Windows never delivers SIGTERM")
def test_a_sweep_sent_sigterm_stops_its_engine_and_dies_by_the_signal(
        tmp_path):
    """The whole path, with the signal really delivered: the handler is
    installed, the engine is told and waited for, its run's group is gone
    and its copy removed, and only then does the sweep exit by the signal."""
    # A project of its own: the fixture's tests would kill the mutant before
    # the slow one ran.
    root = str(tmp_path / "proj")
    pid_file = str(tmp_path / "run.pid")
    files = {rel: text for rel, text in FILES.items()
             if rel not in ("pkg/tests/test_gate.py", "pkg/tests/helpers.py")}
    write_tree(root, {**files, "pyproject.toml": ("[project]\nname = 'x'\n"
                                          "[tool.pytest.ini_options]\n"),
                      "pkg/tests/test_slow.py": SLOW_TEST % pid_file})
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "invective", "sweep", "--src", "pkg",
         "--tests-dir", "pkg/tests", "--only", "RAISE",
         "--modules", "pkg/gate.py"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": SRC})
    run = None
    try:
        run = int(wait_for(pid_file).split()[0])
        sent = time.monotonic()
        os.kill(proc.pid, signal.SIGTERM)

        assert proc.wait(timeout=60) == -signal.SIGTERM
        # The engine was told at once. Left to the escalation, which waits
        # out the whole grace first, the sweep would still end the same way.
        assert time.monotonic() - sent < sweep._GRACE
        with pytest.raises(ProcessLookupError):
            os.killpg(run, 0)
        assert [n for n in os.listdir(tmp_path)
                if n.startswith("invective-")] == []
    finally:
        if proc.poll() is None:
            proc.kill()
        if run is not None:
            stop_group(run)
        proc.communicate()


def test_an_engine_that_outlasts_its_grace_is_told_again_and_then_killed(
        repo, monkeypatch):
    """An engine that does not end within the grace is terminated once more
    (its handler makes that harmless when it was told already), given the
    grace again, and only then killed outright and waited for."""
    engine = _Engine()
    waits = []

    def wait(timeout=None):
        engine.calls.append("wait")
        waits.append(timeout)
        if len(waits) <= 2:
            raise subprocess.TimeoutExpired("engine", timeout)

    def communicate(timeout=None):
        raise mutate._Terminated(signal.SIGTERM)

    engine.wait, engine.communicate = wait, communicate
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)
    monkeypatch.setattr(sweep, "_exit_by", lambda exc: None)
    monkeypatch.setattr(sweep, "_GRACE", 0.01)

    sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                "--modules", _p("pkg/gate.py")])

    assert engine.calls == ["terminate", "wait", "terminate", "wait",
                            "kill", "wait"]
    assert waits == [0.01, 0.01, None]


@pytest.mark.parametrize("raised, told", [
    (KeyboardInterrupt, []),
    (RuntimeError, ["terminate"]),
])
def test_an_interrupted_sweep_waits_for_the_engine_and_the_interrupt_goes_on(
        repo, monkeypatch, raised, told):
    """A ^C reached the engine too, in the same foreground group, so it is
    only waited for: told to stop as well, its own cleanup would be cut
    short. Anything else the engine has not heard of. Either way the
    exception comes out once the engine is gone, as it would have."""
    engine = _Engine()

    def communicate(timeout=None):
        raise raised()

    engine.communicate = communicate
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)

    with pytest.raises(raised):
        sweep.main(["--src", "pkg", "--tests-dir", TESTS_DIR,
                    "--modules", _p("pkg/gate.py")])
    assert engine.calls == told + ["wait"]


def test_the_sweep_runs_from_a_subdirectory_of_the_project(tree, monkeypatch,
                                                           tmp_path):
    """From `pkg` the engine is started at the project's top, given the
    module from there. Started in `pkg`, it copies `pkg` alone, which holds
    no `pkg` to import, and the baseline is red."""
    monkeypatch.chdir(os.path.join(tree, "pkg"))
    out = str(tmp_path / "sweep.json")

    assert sweep.main(["--src", ".", "--tests-dir", "tests", "--modules",
                       "gate.py", "--json", out]) == 0

    with open(out, encoding="utf-8") as fh:
        (entry,) = json.load(fh)["measured"]
    assert entry["module"] == _p("pkg/gate.py")
    assert entry["tests"] == [_p("pkg/tests/test_gate.py")]
    assert (entry["killed"], entry["mutants"]) == (1, 1)


def test_a_sweep_whose_pytest_settings_are_above_the_project_is_refused(
        tmp_path, monkeypatch, capsys):
    """Said once, before any module is measured under other settings."""
    top = os.path.realpath(tmp_path / "ws")
    write_tree(top, WORKSPACE)
    monkeypatch.chdir(os.path.join(top, "m"))

    assert sweep.main(["--src", "pkg", "--tests-dir", "tests"]) == 2
    said = capsys.readouterr()
    assert "refused: pytest reads %s" % os.path.join(top, "pyproject.toml") in (
        said.err)
    assert "measured" not in said.out


def test_a_ref_s_settings_above_the_project_in_its_repository_are_no_refusal(
        tmp_path, monkeypatch, capsys):
    """The repository's `pytest.ini` is above the project, and in the ref's
    tree too, which the engine reads it from: the sweep starts it on a ref,
    and refuses only on the files as they stand, which no copy of the
    project would hold."""
    no_pytest_settings_above(tmp_path)
    mono = os.path.realpath(tmp_path / "mono")
    ref = monorepo(mono, MONOREPO)
    monkeypatch.chdir(os.path.join(mono, "sub"))
    engine = _Engine(0, stdout="1/1 killed (100.0%), 0 survived\n")
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)
    argv = ["--src", "pkg", "--tests-dir", "tests", "--modules",
            _p("pkg/gate.py")]

    assert sweep.main([*argv, "--ref", ref]) == 0
    ((cmd, _kwargs),) = engine.commands
    assert cmd[cmd.index("--ref") + 1] == ref

    assert sweep.main(argv) == 2
    assert "refused: pytest reads %s, which is above the project's top" % (
        os.path.join(mono, "pytest.ini")) in capsys.readouterr().err
    assert len(engine.commands) == 1


def test_settings_above_a_repository_refuse_a_ref_once(tmp_path, monkeypatch,
                                                       capsys):
    """No ref's tree holds a `pytest.ini` above the repository's top: said
    once, with no engine started, as on the files as they stand."""
    no_pytest_settings_above(tmp_path)
    outer = os.path.realpath(tmp_path / "outer")
    mono = os.path.join(outer, "mono")
    write_tree(outer, {"pytest.ini": MONOREPO["pytest.ini"]})
    ref = monorepo(mono, {rel: text for rel, text in MONOREPO.items()
                          if rel != "pytest.ini"})
    monkeypatch.chdir(os.path.join(mono, "sub"))
    engine = _Engine(0, stdout="1/1 killed (100.0%), 0 survived\n")
    monkeypatch.setattr(sweep.subprocess, "Popen", engine)

    assert sweep.main(["--src", "pkg", "--tests-dir", "tests",
                       "--ref", ref]) == 2
    said = capsys.readouterr()
    assert ("refused: pytest reads %s, which is above the top of the "
            "repository %s" % (os.path.join(outer, "pytest.ini"), mono)
            in said.err)
    assert engine.commands == []
    assert "measured" not in said.out
