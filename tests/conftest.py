"""The `project` fixture: a fixture project under `tests/projects/`, run by a real pytest.

Each run is `pytest --mutate` in a copy of its own, so a run cannot change the
project's files or another run's. The project may be invective's own checkout.
"""

from __future__ import annotations

import itertools
import json
import os
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import NamedTuple

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.join(HERE, "projects")

#: The name `project` takes for invective's own checkout.
CHECKOUT = "invective"
_ROOT = os.path.dirname(HERE)
_IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc")


class Run(NamedTuple):
    """One `pytest --mutate` run of a fixture project, after it ended."""

    returncode: int
    out: str  #: stdout and stderr together
    report: dict | None  #: the JSON report, when the run wrote one
    pid: int  #: the pytest process, the parent of every child
    root: str  #: the copy the run was made in

    def verdicts(self) -> dict[str, tuple[str, str | None]]:
        """Return each mutant id's verdict and killer; fails when there is no report."""
        assert self.report is not None, self.out
        return {m["id"]: (m["verdict"], m["killer"]) for m in self.report["mutants"]}


@pytest.fixture
def project(tmp_path: pathlib.Path) -> Callable[..., Run]:
    """Return a function that runs `pytest --mutate` on a copy of a fixture project.

    The function takes the project's name and pytest's further arguments. With
    *files*, a relative path to its text, it writes those files into the copy
    first; with *targets*, it mutates those paths instead of the project's package;
    *env* adds to the environment the run inherits. Each call gets a copy of its
    own.

    The name `CHECKOUT` copies invective's own `src`, `tests` and `pyproject.toml`,
    and the run imports invective from the copy.
    """

    def run(
        name: str,
        *args: str,
        env: dict[str, str] | None = None,
        targets: tuple[str, ...] = (),
        files: dict[str, str] | None = None,
    ) -> Run:
        """Copy the project *name*, run pytest in the copy, and return the run."""
        root = str(tmp_path / name)
        for n in itertools.count(2):
            if not os.path.exists(root):
                break
            root = str(tmp_path / f"{name}-{n}")
        if name == CHECKOUT:
            for part in ("src", "tests"):
                shutil.copytree(
                    os.path.join(_ROOT, part), os.path.join(root, part), ignore=_IGNORED
                )
            shutil.copy(os.path.join(_ROOT, "pyproject.toml"), root)
            # Ahead of the installed invective, so that the plugin pytest loads, and
            # every module it imports, is the copy's.
            env = {"PYTHONPATH": os.path.join(root, "src"), **(env or {})}
        else:
            shutil.copytree(os.path.join(PROJECTS, name), root, ignore=_IGNORED)
        for relative, text in (files or {}).items():
            os.makedirs(os.path.dirname(os.path.join(root, relative)), exist_ok=True)
            with open(os.path.join(root, relative), "w", encoding="utf-8") as fh:
                fh.write(text)

        argv = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--mutate-json",
            "report.json",
        ]
        for target in targets or _default_targets(name):
            argv += ["--mutate", target]
        argv += list(args)
        environment = {**os.environ, "COLUMNS": "200", **(env or {})}
        environment.pop("PYTEST_ADDOPTS", None)
        proc = subprocess.Popen(
            argv,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
        out, _ = proc.communicate(timeout=120)

        report = None
        if os.path.exists(os.path.join(root, "report.json")):
            with open(os.path.join(root, "report.json"), encoding="utf-8") as fh:
                report = json.load(fh)
        return Run(proc.returncode, out, report, proc.pid, root)

    return run


def _default_targets(name: str) -> tuple[str, ...]:
    """Return what a run of the project *name* mutates by default: its package."""
    return (os.path.join("src", name),)
