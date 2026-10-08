"""Which pytest settings every run of a campaign goes by, through `invective
run`, `invective sweep` and `pytest --mutate` alike, recorded by the runs
themselves."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import SRC, commit, git, no_pytest_settings_above, write_tree

GATE = ("def admit(age):\n"
        "    if age < 18:\n"
        "        raise ValueError('under age')\n"
        "    return age\n")
#: Only a strict xfail fails on the gate's one `RAISE` mutant, so the score
#: says whether `xfail_strict` was read.
TESTS = ("import pytest\n"
         "from pkg import gate\n"
         "\n"
         "@pytest.mark.xfail(raises=ValueError)\n"
         "def test_a_minor_is_refused():\n"
         "    gate.admit(10)\n"
         "\n"
         "def test_an_adult_is_let_in():\n"
         "    assert gate.admit(30) == 30\n")
#: Each run's own account of what it read, a JSON line to the file `READS`
#: names: its arguments, working directory, settings file, rootdir,
#: `conftest.py` files and strictness.
RECORDER = (
    "import json, os, sys\n"
    "\n"
    "def pytest_configure(config):\n"
    "    if 'READS' not in os.environ:\n"
    "        return\n"
    "    conftests = sorted(\n"
    "        str(plugin.__file__)\n"
    "        for plugin in config.pluginmanager.get_plugins()\n"
    "        if getattr(plugin, '__name__', '').endswith('conftest'))\n"
    "    with open(os.environ['READS'], 'a', encoding='utf-8') as fh:\n"
    "        fh.write(json.dumps({\n"
    "            'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
    "            'ini': str(config.inipath or ''),\n"
    "            'rootdir': str(config.rootpath), 'conftests': conftests,\n"
    "            'strict': bool(config.getini('xfail_strict'))}) + '\\n')\n")
_STRICT = "[pytest]\nxfail_strict = true\n"
_STRICT_PYPROJECT = ("[project]\nname = 'p'\n[tool.pytest.ini_options]\n"
                     "xfail_strict = true\n")
_TABLELESS = "[project]\nname = 'p'\n"

#: Layouts issue #1 asks no change of: (files beside the project's own, the
#: project's directory, the tests as typed, whether through a ref) and what
#: every run of 5241e5f read in each, through all three, its paths given
#: from the top of the tree it ran in: (working directory, settings file,
#: rootdir, conftests, strict). In the two `tests/pytest.ini` layouts
#: 5241e5f's plugin read the top's `pyproject.toml` instead; issue #1 asks
#: the three to agree, and they read what its `invective run` read.
CLEAN = {
    "pyproject-table": (
        {"pyproject.toml": _STRICT_PYPROJECT}, "", ["tests"], False,
        (".", "pyproject.toml", ".", ["tests/conftest.py"], True)),
    "pytest.ini": (
        {"pytest.ini": _STRICT, "pyproject.toml": _TABLELESS}, "", ["tests"],
        False, (".", "pytest.ini", ".", ["tests/conftest.py"], True)),
    "pytest.toml": (
        {"pytest.toml": _STRICT}, "", ["tests"], False,
        (".", "pytest.toml", ".", ["tests/conftest.py"], True)),
    "setup.cfg": (
        {"setup.cfg": "[tool:pytest]\nxfail_strict = true\n"}, "", ["tests"],
        False, (".", "setup.cfg", ".", ["tests/conftest.py"], True)),
    "tox.ini": (
        {"tox.ini": _STRICT}, "", ["tests"], False,
        (".", "tox.ini", ".", ["tests/conftest.py"], True)),
    "tableless-pyproject": (
        {"pyproject.toml": _TABLELESS}, "", ["tests"], False,
        (".", "pyproject.toml", ".", ["tests/conftest.py"], False)),
    "tests/pytest.ini": (
        {"pyproject.toml": _TABLELESS, "tests/pytest.ini": _STRICT}, "",
        ["tests"], False,
        (".", "tests/pytest.ini", "tests", ["tests/conftest.py"], True)),
    "tests/pytest.ini-file-arg": (
        {"pyproject.toml": _TABLELESS, "tests/pytest.ini": _STRICT}, "",
        ["tests/test_gate.py"], False,
        (".", "tests/pytest.ini", "tests", ["tests/conftest.py"], True)),
    "repository-only": (
        {".git/HEAD": "ref: refs/heads/main\n"}, "", ["tests"], False,
        (".", "", ".", ["tests/conftest.py"], False)),
    "top-conftest": (
        {"pyproject.toml": _STRICT_PYPROJECT, "conftest.py": ""}, "",
        ["tests"], False,
        (".", "pyproject.toml", ".", ["conftest.py", "tests/conftest.py"],
         True)),
    "ref-monorepo": (
        {"pytest.ini": _STRICT, "sub/pyproject.toml": _TABLELESS}, "sub",
        ["tests"], True,
        ("sub", "pytest.ini", ".", ["sub/tests/conftest.py"], True)),
    "ref-own": (
        {"sub/pyproject.toml": _STRICT_PYPROJECT}, "sub", ["tests"], True,
        ("sub", "sub/pyproject.toml", "sub", ["sub/tests/conftest.py"],
         True)),
}

#: invective's own arguments to every run, 5241e5f's but for where the
#: plugin's `-p` stands: no settings file is ever named.
_BUILDER = ["-x", "-rf", "-p", "no:randomly", "-n", "0", "--no-header"]


def _build(top, files, sub, ref):
    """The layout at *top*: the gate, its tests and the recorder in the
    project's directory *sub*, beside *files*; committed for a ref."""
    project = os.path.join(top, sub)
    write_tree(top, {rel: text for rel, text in files.items()
                     if not rel.startswith(".git/")})
    write_tree(project, {"pkg/__init__.py": "", "pkg/gate.py": GATE,
                         "tests/test_gate.py": TESTS,
                         "tests/conftest.py": RECORDER})
    if ref or ".git/HEAD" in files:
        git(top, "init", "-q", "-b", "main")
    if ref:
        commit(top)
    return project


def _campaign(entry, project, tests, ref, tmp_path):
    """One campaign through *entry*, `--only RAISE`: its exit code, the
    first refusal it gave, its score, and every run's record."""
    reads, out = tmp_path / "reads.jsonl", tmp_path / "report.json"
    for left in (reads, out):
        if left.exists():
            left.unlink()
    command = {
        "run": ["-m", "invective", "run", "--target", "pkg/gate.py",
                "--tests", *tests, "--only", "RAISE", "--json", str(out)],
        "sweep": ["-m", "invective", "sweep", "--src", "pkg", "--tests-dir",
                  "tests", "--modules", os.path.join("pkg", "gate.py"),
                  "--only", "RAISE", "--json", str(out)],
        "plugin": ["-m", "pytest", "-p", "no:cacheprovider",
                   "--mutate=pkg/gate.py", "--mutate-only", "RAISE",
                   "--mutate-json", str(out), *tests],
    }[entry]
    if ref:
        command += ["--mutate-ref" if entry == "plugin" else "--ref", "HEAD"]
    done = subprocess.run([sys.executable, *command], cwd=project,
                          capture_output=True, text=True, timeout=300,
                          env={**os.environ, "PYTHONPATH": SRC,
                               "READS": str(reads)})
    said = (done.stdout + done.stderr).splitlines()
    refused = next((line.partition("refused: ")[2] for line in said
                    if "refused: " in line), None)
    score = None
    if out.exists():
        report = json.loads(out.read_text(encoding="utf-8"))
        if entry == "plugin":
            (report,) = report
        if entry == "sweep":
            (report,) = [row for row in report["measured"]
                         if row["module"].endswith("gate.py")]
        score = (report["killed"], report["mutants"])
    runs = []
    if reads.exists():
        runs = [json.loads(line) for line
                in reads.read_text(encoding="utf-8").splitlines()]
    return done, refused, score, runs


def _given(path, top):
    """*path*, as a run saw it, from *top*, the top of the tree it ran in;
    `/`-separated, so that a table holds it on every platform."""
    if not path:
        return ""
    rel = os.path.relpath(os.path.normcase(os.path.realpath(path)),
                          os.path.normcase(os.path.realpath(top)))
    return rel.replace(os.sep, "/")


def _read(run, tree):
    """What *run* read, given from *tree*, as `CLEAN` holds it."""
    return (_given(run["cwd"], tree), _given(run["ini"], tree),
            _given(run["rootdir"], tree),
            [_given(path, tree) for path in run["conftests"]], run["strict"])


@pytest.mark.parametrize("entry", ["run", "sweep", "plugin"])
@pytest.mark.parametrize("name", list(CLEAN))
def test_every_run_reads_what_5241e5f_s_read(tmp_path, name, entry):
    """Where issue #1 asks no change, every run, the baseline's and the
    mutant's, reads the settings, rootdir and `conftest.py` files 5241e5f's
    did, is given no settings file, and gives 5241e5f's score."""
    files, sub, tests, ref, read = CLEAN[name]
    if "pytest.toml" in files and int(pytest.__version__.split(".")[0]) < 9:
        pytest.skip("pytest reads pytest.toml from 9 on")
    if entry == "sweep" and tests != ["tests"]:
        pytest.skip("the sweep finds its tests itself")
    no_pytest_settings_above(tmp_path)
    top = os.path.realpath(tmp_path / "layout")
    project = _build(top, files, sub, ref)

    done, _refused, score, runs = _campaign(entry, project, tests, ref,
                                            tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr
    assert score == ((1, 1) if read[4] else (0, 1))
    if entry == "plugin":
        # The plugin's own pytest, in the project as it stands.
        outer, *runs = runs
        assert _read(outer, top) == read
    # The baseline's and the one mutant's.
    assert len(runs) == 2
    given = [os.path.join(*path.split("/")) for path in (
        ["tests/test_gate.py"] if entry == "sweep" else tests)]
    for run in runs:
        # A ref's project is a directory below its tree's top.
        assert _read(run, os.path.dirname(run["cwd"]) if sub
                     else run["cwd"]) == read
        argv = list(run["argv"])
        at = argv.index("pytest_invective")
        del argv[at - 1:at + 1]
        # The plugin's runs are given its forwarded options too.
        forwarded = (["-p", "no:cacheprovider", "--import-mode=prepend"]
                     if entry == "plugin" else [])
        assert argv == [*forwarded, *_BUILDER, *given]


#: Layouts where the project's own pytest reads something from above the
#: project's top, or where what it reads is left out of the copy: (files,
#: the project's directory, the repository's directory for a ref or None,
#: what every entry point gives: the file its refusal names, or the score).
AROUND = {
    "a-monorepo-s-settings-above": (
        {"pytest.ini": _STRICT, "sub/pyproject.toml": _TABLELESS}, "sub",
        None, "pytest.ini"),
    "a-monorepo-s-markers-above": (
        {"pytest.ini": "[pytest]\nmarkers =\n    slow: a slow test\n",
         "sub/pyproject.toml": _TABLELESS}, "sub", None, (0, 1)),
    "settings-above-a-ref-s-repository": (
        {"pytest.ini": _STRICT, "repo/sub/pyproject.toml": _TABLELESS},
        "repo/sub", "repo", "pytest.ini"),
    "a-conftest-below-a-tool-s-pyproject": (
        {"pyproject.toml": "[tool.ruff]\n", "w/conftest.py": "",
         "w/sub/setup.py": ""}, "w/sub", None, "w/conftest.py"),
    "a-tool-s-pyproject-above": (
        {"pyproject.toml": "[tool.ruff]\n",
         "sub/setup.cfg": "[metadata]\nname = s\n"}, "sub", None, (0, 1)),
    # The copy's own settings decide, as at 5241e5f: issue #2.
    "settings-the-copy-leaves-out": (
        {"pytest.ini": _STRICT,
         "pyproject.toml": (_TABLELESS + "[tool.pytest.ini_options]\n"
                            "[tool.invective]\nexclude = ['pytest.ini']\n")},
        "", None, (0, 1)),
}


@pytest.mark.parametrize("name", list(AROUND))
def test_run_the_sweep_and_the_plugin_agree(tmp_path, name):
    """The same refusal, word for word, or the same score, from each."""
    files, sub, repository, expected = AROUND[name]
    top = os.path.realpath(tmp_path / "layout")
    no_pytest_settings_above(tmp_path)
    write_tree(top, files)
    project = os.path.join(top, *sub.split("/"))
    write_tree(project, {"pkg/__init__.py": "", "pkg/gate.py": GATE,
                         "tests/test_gate.py": TESTS})
    if repository is not None:
        git(os.path.join(top, repository), "init", "-q", "-b", "main")
        commit(os.path.join(top, repository))

    outcomes = []
    for entry in ("run", "sweep", "plugin"):
        done, refused, score, _runs = _campaign(
            entry, project, ["tests"], repository is not None, tmp_path)
        assert done.returncode == (0 if score else 2), (
            done.stdout + done.stderr)
        outcomes.append(refused or score)
    assert outcomes[0] == outcomes[1] == outcomes[2]
    if isinstance(expected, tuple):
        assert outcomes[0] == expected
    else:
        assert outcomes[0].startswith("pytest reads %s, which is above " %
                                      os.path.join(top, *expected.split("/")))
