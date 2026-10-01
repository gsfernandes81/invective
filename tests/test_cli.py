"""The `invective` command: it hands each subcommand its own arguments."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from invective import __main__ as cli

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")


@pytest.mark.parametrize("name", ["run", "sweep"])
def test_a_subcommand_gets_the_arguments_after_its_name(monkeypatch, name):
    got = []
    monkeypatch.setitem(cli.COMMANDS, name, lambda argv: got.append(argv) or 7)

    assert cli.main([name, "--limit", "3"]) == 7
    assert got == [["--limit", "3"]]


@pytest.mark.parametrize("argv", [[], ["mutate"], ["--target", "m.py"]])
def test_anything_else_is_a_usage_error_and_runs_nothing(monkeypatch, capsys,
                                                         argv):
    for name in cli.COMMANDS:
        monkeypatch.setitem(cli.COMMANDS, name,
                            lambda argv: pytest.fail("ran a subcommand"))

    assert cli.main(argv) == 2
    assert "usage: invective run" in capsys.readouterr().err


def test_the_module_runs_as_a_command():
    """`python -m invective`, in a process of its own."""
    done = subprocess.run([sys.executable, "-m", "invective", "--help"],
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": SRC})

    assert done.returncode == 0, done.stderr
    assert "invective sweep" in done.stdout
