"""`--no-unsafe-speedups`: every speedup on its side, and with the unsafe
ones off, each set off whatever the settings file says, and a flag that sets
one on refused.

A speedup is unsafe when it can give a wrong verdict on a suite whose tests
depend on their order or are not safe to run in parallel. A new one declares
its side in `config.SPEEDUPS`, and the first test here holds that every
setting has.
"""

from __future__ import annotations

import os

import pytest

from invective import config, mutate

from conftest import write_tree
from test_workers import GATE, GATE_TESTS, GREEN, Campaign


def _main(tree, monkeypatch, *args, setting=""):
    write_tree(tree, {"pyproject.toml": "[tool.pytest.ini_options]\n"
                      "[tool.invective]\n" + setting})
    given = []
    monkeypatch.setattr(mutate, "mutate", lambda *a, **k: given.append(k) or {
        "killed": 0, "mutants": 1, "survivors": [], "accepted": [],
        "stale": [], "kills": [], "broken": 0})
    monkeypatch.chdir(tree)
    code = mutate.main(["--target", GATE, "--tests", *GATE_TESTS, *args])
    return code, given


@pytest.mark.parametrize("args, setting", [
    (["--no-unsafe-speedups"], "workers = 4\nhistory = true\n"),
    ([], "workers = 4\nunsafe-speedups = false\n"),
    (["--no-unsafe-speedups", "--workers", "1"], ""),
    # One worker as `--workers` reads it, whatever its spelling.
    (["--no-unsafe-speedups", "--workers", "01"], ""),
    (["--workers", "1"], "unsafe-speedups = false\n")])
def test_the_switch_overrides_the_settings_file(tree, monkeypatch, args,
                                               setting):
    code, given = _main(tree, monkeypatch, *args, setting=setting)
    assert code == 0
    (kwargs,) = given
    assert (kwargs["workers"], kwargs["history"],
            kwargs["unsafe_speedups"]) == (1, False, False)


def test_without_the_switch_the_settings_and_flags_stand(tree, monkeypatch):
    code, given = _main(tree, monkeypatch, "--workers", "3",
                        setting="history = false\n")
    assert code == 0
    (kwargs,) = given
    assert (kwargs["workers"], kwargs["history"],
            kwargs["unsafe_speedups"]) == ("3", False, True)
    code, given = _main(tree, monkeypatch)
    assert (given[0]["workers"], given[0]["history"]) == (1, True)


@pytest.mark.parametrize("args, setting, says", [
    (["--no-unsafe-speedups", "--workers", "4"], "",
     "--workers 4 runs mutants at once, and --no-unsafe-speedups turns that "
     "off"),
    (["--workers", "auto"], "unsafe-speedups = false\n",
     "--workers auto runs mutants at once, and [tool.invective] "
     "unsafe-speedups = false turns that off"),
    (["--no-unsafe-speedups", "--workers", "2", "--confirm"], "",
     "--workers 2 runs mutants at once")])
def test_a_flag_that_sets_an_unsafe_speedup_on_with_them_off_is_refused(
        tree, monkeypatch, capsys, args, setting, says):
    # "auto" asks for as many workers as the machine has, so it is refused
    # on a machine of one CPU too.
    monkeypatch.setattr(config, "_cpus", lambda: 1)
    code, given = _main(tree, monkeypatch, *args, setting=setting)
    assert (code, given) == (2, [])
    assert says in capsys.readouterr().err


def test_every_setting_is_a_speedup_on_its_side_or_none(tmp_path):
    """A new setting is either a speedup, safe or unsafe, or none: one that
    is neither here is one `--no-unsafe-speedups` has not been told about."""
    speedups = {s.field for s in config.SPEEDUPS if s.field is not None}
    named = {key.replace("-", "_") for key in config._KEYS}
    assert named - speedups == {"fail_on_survivors", "max_accepted",
                                "exclude", "confirm", "unsafe_speedups"}
    assert speedups <= named
    assert {s.field: s.unsafe for s in config.SPEEDUPS} == {
        None: False, "workers": True, "history": True, "coverage": True}


def test_settle_turns_off_each_unsafe_speedup_and_keeps_the_rest():
    rules = config.Config(workers="auto", history=True, confirm=True,
                          unsafe_speedups=False)
    assert config.settle(rules) == rules._replace(workers=1, history=False)
    assert config.settle(rules._replace(unsafe_speedups=True)) == rules._replace(
        unsafe_speedups=True)
    assert config.settle(config.Config(workers=4), switch="--x") == (
        config.Config(workers=1, history=False, unsafe_speedups=False))
    assert config.settle(config.Config(), {"workers": ("--w", "3")}) == (
        config.Config(workers="3"))


def test_a_direct_call_holds_every_speedup_to_the_switch(tree, monkeypatch):
    """Every speedup of `config.SPEEDUPS` reaches `settle` as a direct
    caller gave it, one registered after the engine was written as well
    (here a stand-in on `confirm`), and the run goes by what `settle` leaves
    of each: one worker, and no history."""
    extra = config.Speedup("confirms each kill alone", "confirm", True, False)
    speedups = config.SPEEDUPS + (extra,)
    monkeypatch.setattr(config, "SPEEDUPS", speedups)
    monkeypatch.setattr(mutate, "SPEEDUPS", speedups)
    run = Campaign(tree, monkeypatch, said=lambda r: GREEN)
    given = []
    real = mutate.settle
    monkeypatch.setattr(mutate, "settle", lambda settings, *rest: (
        given.append(settings), real(settings, *rest))[1])
    on = {s.field: (not s.off) if isinstance(s.off, bool) else s.off + 1
          for s in speedups if s.field is not None}
    report = run(**on, unsafe_speedups=False)
    (settings,) = given
    assert {field: getattr(settings, field) for field in on} == on
    assert report["workers"] == 1 and len(run.places) == 1
    assert not os.path.exists(os.path.join(tree, ".invective"))
