"""The fixture projects under `tests/projects/`, each run by a real pytest
with `--mutate`, in a copy of its own."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
from typing import NamedTuple

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.join(HERE, "projects")


class Run(NamedTuple):
    returncode: int
    out: str
    report: dict | None      #: the JSON report, when the run wrote one
    pid: int                 #: the pytest process, the parent of every child
    root: str                #: the copy the run was made in

    def verdicts(self) -> dict[str, tuple[str, str | None]]:
        """Mutant id to (verdict, killer)."""
        assert self.report is not None, self.out
        return {m["id"]: (m["verdict"], m["killer"]) for m in self.report["mutants"]}


@pytest.fixture
def project(tmp_path):
    """A function: copy the named project, write *files* (a relative path
    to its text) into the copy, and run pytest --mutate in it. Each call
    gets a copy of its own."""

    def run(name: str, *args: str, env: dict[str, str] | None = None,
            targets: tuple[str, ...] = (), files: dict[str, str] | None = None) -> Run:
        root = str(tmp_path / name)
        for n in itertools.count(2):
            if not os.path.exists(root):
                break
            root = str(tmp_path / f"{name}-{n}")
        shutil.copytree(os.path.join(PROJECTS, name), root,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for relative, text in (files or {}).items():
            os.makedirs(os.path.dirname(os.path.join(root, relative)), exist_ok=True)
            with open(os.path.join(root, relative), "w", encoding="utf-8") as fh:
                fh.write(text)
        argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                "--mutate-json", "report.json"]
        for target in targets or _default_targets(name):
            argv += ["--mutate", target]
        argv += list(args)
        environment = {**os.environ, "COLUMNS": "200", **(env or {})}
        environment.pop("PYTEST_ADDOPTS", None)
        proc = subprocess.Popen(argv, cwd=root, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=environment)
        out, _ = proc.communicate(timeout=120)
        report = None
        if os.path.exists(os.path.join(root, "report.json")):
            with open(os.path.join(root, "report.json"), encoding="utf-8") as fh:
                report = json.load(fh)
        return Run(proc.returncode, out, report, proc.pid, root)

    return run


def _default_targets(name: str) -> tuple[str, ...]:
    return (os.path.join("src", name),)
