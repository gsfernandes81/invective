"""invective run on its own checkout: what it mutates, and what it leaves out.

The files the plugin imports before the finder exists are left out and named in the
report, and the code marked `switch.harness`, which a child runs between the fork and
its verdict, has no mutant.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

from invective import plugin, report

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Per file, the qualnames marked `switch.harness`; a class's methods go with it.
MARKED = {
    "src/invective/baseline.py": {"_Prescan", "_cpu"},
    "src/invective/children.py": {"_flush_stdio", "fork", "run_all.<locals>.undo"},
    "src/invective/mutants.py": {"_activate", "_child_run"},
    "src/invective/protocol.py": {"Recorder", "recapture", "run_items", "teardown"},
}


def test_invective_mutates_the_report_its_engine_writes(project):
    """A run on the checkout mutates `report`, which the plugin imports for the engine.

    The plugin imports the engine and the report after the finder is installed, so
    neither is refused as imported before invective could instrument it.
    """
    run = project(
        "invective",
        "-n",
        "0",
        "tests/test_report.py",
        targets=("src/invective/report.py",),
    )
    assert run.returncode == 0, run.out
    assert "imported before invective could instrument it" not in run.out
    mutants = run.report["mutants"]
    assert {m["function"] for m in mutants} == {"lines", "_where", "_said", "write_json"}
    assert {m["verdict"] for m in mutants} <= set(report.VERDICTS)
    assert run.report["canary"].startswith("tests/test_report.py::")


def test_the_harness_is_left_out_of_a_run_on_invectives_package(project):
    """A run on the whole package leaves out the harness and names each file left out.

    The files imported before the finder exists are skipped, with the reason, in the
    JSON file and on the console; a function marked `switch.harness` has no mutant,
    and every other function of its module does.
    """
    run = project("invective", "-n", "0", "tests/test_tracer.py")
    assert run.returncode == 0, run.out
    skipped = [f"src/invective/{name}" for name in sorted(plugin.HARNESS)]
    assert run.report["skipped"] == [
        {"file": path, "reason": report.SKIPPED} for path in skipped
    ]
    for path in skipped:
        assert f"{path}  skipped: {report.SKIPPED}" in run.out
    assert "src/invective/finder.py" not in run.report["targets"]

    mutants = run.report["mutants"]
    assert {m["verdict"] for m in mutants} <= set(report.VERDICTS)
    for m in mutants:
        marked = MARKED.get(m["file"], set())
        assert not any(
            m["function"] == name or m["function"].startswith(f"{name}.")
            for name in marked
        ), m["id"]
    protocol = {
        m["function"] for m in mutants if m["file"] == "src/invective/protocol.py"
    }
    assert protocol == {"read_events"}
    assert run.report["canary"].startswith("tests/test_tracer.py::")


def test_a_run_naming_only_the_harness_is_a_usage_error(project):
    """A run whose every file named is part of the harness has nothing to mutate."""
    run = project(
        "invective",
        "-n",
        "0",
        "tests/test_report.py",
        targets=("src/invective/finder.py", "src/invective/switch.py"),
    )
    assert run.returncode == 4, run.out
    assert "every file named is part of invective's harness" in run.out


# Run as a script in a fresh interpreter, so that nothing of invective is imported
# before pytest loads the plugin.
_WATCH = """\
import json
import sys

import pytest

early = set()


class Watch:
    def find_spec(self, name, path=None, target=None):
        finder = any(type(f).__module__ == "invective.finder" for f in sys.meta_path)
        if name.partition(".")[0] == "invective" and not finder:
            early.add(name)


sys.meta_path.insert(0, Watch())
pytest.main(sys.argv[1:])
print(json.dumps(sorted(early)))
"""


def test_the_harness_is_what_the_plugin_imports_before_the_finder_exists(
    tmp_path: pathlib.Path,
) -> None:
    """`plugin.HARNESS` is every module of invective imported before the finder exists.

    A module imported earlier and missing from the list would refuse a run that names
    it; one listed but imported later would be left out of a run for nothing.
    """
    (tmp_path / "mod.py").write_text("def one() -> int:\n    return 1\n")
    (tmp_path / "test_mod.py").write_text(
        "import mod\n\n\ndef test_one():\n    assert mod.one() == 1\n"
    )
    (tmp_path / "watch.py").write_text(_WATCH)
    proc = subprocess.run(
        [
            sys.executable,
            "watch.py",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "--mutate",
            "mod.py",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    early = json.loads(proc.stdout.splitlines()[-1])
    expected = {
        ".".join(["invective", *name.removesuffix(".py").split("/")]).removesuffix(
            ".__init__"
        )
        for name in plugin.HARNESS
    }
    assert set(early) == expected
