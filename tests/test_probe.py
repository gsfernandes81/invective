"""The killer history: each mutant's last killer, run alone on the mutant
before the whole selection, once it has passed alone on the original; and
`--no-unsafe-speedups`, which turns it off with every other speedup that
needs the suite to keep the contract.

Most of these drive the engine with `Probing`, `test_workers.Campaign`'s
stand-in for `run_tests` that also knows a probe's file, deciding each run
from the mutant in the copy and the tests the run was given.
"""

from __future__ import annotations

import _thread
import json
import os
import subprocess
import sys
import threading
from typing import NamedTuple

import pytest
from pytest import ExitCode

from invective import mutate, store

from conftest import FILES, SRC
from test_workers import (ADULT, GATE, GATE_FILE, GATE_TESTS, GREEN, MINOR,
                          Campaign, Run, killed_by)
# A fixture: every outcome the engine says stands.
from test_workers import landed  # noqa: F401

SOURCE = FILES["pkg/gate.py"]


class Site(NamedTuple):
    """One mutant of `SOURCE`: what the report calls it, its key in the
    history, and its text."""

    kind: str
    what: str
    line: int
    key: str
    text: str


def _sites(source=SOURCE):
    module = mutate._Module(source)
    keys = store.mutant_keys(mutate._report_lines(source), module.sites)
    return [Site(kind, what, node.lineno, keys[i], mutate._text_of(module, i)[0])
            for i, (kind, node, what) in enumerate(module.sites)]


SITES = {site.what: site for site in _sites()}
RAISE = "raise ... -> pass"
BOOL = "And -> Or"


def remember(tree, killers, target=GATE_FILE):
    """The history of *tree*: each mutant of `SITES` named by its change,
    with its killer."""
    os.makedirs(os.path.join(tree, ".invective"), exist_ok=True)
    with open(os.path.join(tree, ".invective", "history.json"), "w",
              encoding="utf-8") as fh:
        json.dump({target: {SITES[what].key: killer
                            for what, killer in killers.items()}}, fh)


def remembered(tree, target=GATE_FILE):
    """What *tree*'s history says now, by change."""
    path = os.path.join(tree, ".invective", "history.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh).get(target, {})
    by_key = {site.key: what for what, site in SITES.items()}
    return {by_key.get(key, key): killer for key, killer in data.items()}


def world(fails, red_alone=()):
    """A suite of `MINOR` then `ADULT` that keeps the contract: *fails*
    names, for a mutant by its change, the tests that fail on it, and a
    test fails alone as it does in the selection. The original passes,
    but for a test of *red_alone* run alone."""
    by_text = {site.text: site.what for site in SITES.values()}

    def said(run):
        if run.text is None:
            return killed_by(run.alone) if run.alone in red_alone else GREEN
        failing = fails.get(by_text[run.text], ())
        if run.alone:
            return killed_by(run.alone) if run.alone in failing else GREEN
        for test in (MINOR, ADULT):
            if test in failing:
                return killed_by(test)
        return GREEN

    return said


class Probing(Campaign):
    """A `Campaign` with the history on, whose stand-in also takes a probe's
    file as a run of one test alone, and gives the tests a run kept when it
    is asked for them. *files* holds each run's kind of selection file:
    `probe`, `killer` (confirmation's) or `""` for the selection."""

    def __init__(self, tree, monkeypatch, said=None,
                 selected=(MINOR, ADULT)):
        super().__init__(tree, monkeypatch, SOURCE, said)
        self.selected, self.files, self.asked = selected, [], []

    def run_tests(self, where, tests, timeout, selection=None, options=(),
                  target="", stop=None, *, inventory=False):
        self.stop = stop
        with open(os.path.join(where, GATE), encoding="utf-8",
                  newline="") as fh:
            text = fh.read()
        alone, kind = "", ""
        name = os.path.basename(selection or "")
        if name.startswith(("probe-", "killer-")):
            kind = name.partition("-")[0]
            with open(selection, encoding="utf-8") as fh:
                (alone,) = fh.read().splitlines()
        run = Run(self.places.index(where),
                  None if text == self.source else text, alone, timeout)
        with self.lock:
            self.runs.append(run)
            self.files.append(kind)
            self.asked.append(inventory)
        got = self.said(run)
        return got._replace(selected=self.selected) if inventory else got

    def __call__(self, workers=1, only=None, limit=None, **kwargs):
        kwargs.setdefault("history", True)
        return super().__call__(workers, only, limit, **kwargs)

    def probed(self):
        """The changes of the mutants a probe was run on, and its test."""
        by_text = {site.text: site.what for site in SITES.values()}
        return [(by_text[run.text], run.alone)
                for run, kind in zip(self.runs, self.files)
                if kind == "probe" and run.text is not None]

    def gated(self):
        """The tests run alone on the original, in the order they ran."""
        return [run.alone for run, kind in zip(self.runs, self.files)
                if kind == "probe" and run.text is None]


# --------------------------------------------------------------------------
# The probe


def test_a_remembered_killer_is_run_alone_and_its_kill_is_marked(
        tree, monkeypatch):
    remember(tree, {RAISE: MINOR, BOOL: ADULT})
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    report = run()

    assert run.gated() == [MINOR, ADULT]
    assert sorted(run.probed()) == [(BOOL, ADULT), (RAISE, MINOR)]
    (kill,) = report["kills"]
    assert (kill["change"], kill["killer"], kill["via"]) == (RAISE, MINOR,
                                                             "probe")
    # The kill was the probe's: its mutant was not run against the whole
    # selection. The survivor was, after its probe passed.
    by_text = {site.text: site.what for site in SITES.values()}
    whole = [by_text[r.text] for r, kind in zip(run.runs, run.files)
             if r.text is not None and kind == ""]
    assert RAISE not in whole and BOOL in whole
    assert "via" not in next(s for s in report["survivors"]
                             if s["change"] == BOOL)
    assert ("history:   2 remembered, 2 usable") in run.lines
    assert run.lines.index("history:   2 remembered, 2 usable") == (
        run.lines.index("mutants:   6\n") - 1)
    assert mutate.summary(report)[1:] == [
        "           1 of the kills were a remembered killer run alone"]
    # Remembered, and the survivor forgotten.
    assert remembered(tree) == {RAISE: MINOR}


def test_every_gate_run_sees_the_unmutated_target(tree, monkeypatch):
    """A killer is gated on the original, in a copy holding no mutant, and
    at the mutants' budget."""
    remember(tree, {RAISE: MINOR})
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    run(2)
    gates = [r for r, kind in zip(run.runs, run.files)
             if kind == "probe" and r.text is None]
    assert [r.alone for r in gates] == [MINOR]
    assert gates[0].timeout == 30


@pytest.mark.parametrize("took, timeout", [(0.5, 5.0), (2.0, 6.0),
                                           (20.0, 30.0)])
def test_a_probe_s_time_is_bounded_by_five_seconds_and_the_budget(took,
                                                                  timeout):
    runs = []

    class _Copy:
        def run(self, mutant, budget, selection=None):
            runs.append(budget)
            return GREEN

    attempt = mutate.probe_attempt({0: mutate.Probe(MINOR, "p", took)}, 30.0)
    mutant = mutate.Mutant(mutate.Job(1, 0, "RAISE", RAISE, 3), "text", True)
    assert attempt(mutant, _Copy()) is None
    assert runs == [timeout]
    other = mutate.Mutant(mutate.Job(2, 1, "RAISE", RAISE, 3), "text", True)
    assert attempt(other, _Copy()) is None and runs == [timeout]


def test_a_killer_red_alone_on_the_unmutated_tree_is_never_probed(
        tree, monkeypatch):
    """It fails apart from the rest where the whole selection passes, so its
    failing alone on a mutant would say nothing; the header names it."""
    remember(tree, {RAISE: MINOR, BOOL: ADULT})
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]},
                                           red_alone=[MINOR]))
    report = run()
    assert run.probed() == [(BOOL, ADULT)]
    assert [k["change"] for k in report["kills"]] == [RAISE]
    assert "via" not in report["kills"][0]
    assert "history:   2 remembered, 1 usable" in run.lines
    assert ("apart:     1 not green on the original when run apart from the "
            "rest: %s; --no-unsafe-speedups runs only the whole selection"
            % MINOR) in run.lines


def test_the_header_names_three_of_the_tests_not_green_apart():
    names = ["t::a", "t::b", "t::c", "t::d"]
    assert mutate._apart_line(names) == (
        "apart:     4 %s: t::a, t::b, t::c; --no-unsafe-speedups runs only "
        "the whole selection" % mutate.NOT_GREEN_APART)


def test_a_killer_missing_on_the_original_is_never_probed_and_not_named(
        tree, monkeypatch):
    remember(tree, {RAISE: MINOR})
    said = world({RAISE: [MINOR]})
    run = Probing(tree, monkeypatch, lambda r: (
        GREEN._replace(missing=(MINOR,)) if r.alone and r.text is None
        else said(r)))
    report = run()
    assert run.probed() == []
    assert "via" not in report["kills"][0]
    assert "history:   1 remembered, 0 usable" in run.lines
    assert not any(line.startswith("apart:") for line in run.lines)


@pytest.mark.parametrize("probe", [
    GREEN, killed_by(ADULT), killed_by(MINOR)._replace(missing=(MINOR,)),
    mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT"),
    mutate.Verdict(False, ExitCode.INTERRUPTED, "", "pkg/tests/test_gate.py"),
    mutate.Verdict(False, ExitCode.USAGE_ERROR, "", ""),
    killed_by(MINOR, ExitCode.INTERNAL_ERROR)],
    ids=["pass", "another", "missing", "timeout", "code-2", "code-4",
         "code-3"])
def test_a_probe_that_is_not_its_killer_failing_runs_the_full_selection(
        tree, monkeypatch, probe):
    remember(tree, {RAISE: MINOR})
    said = world({RAISE: [MINOR]})
    run = Probing(tree, monkeypatch, lambda r: (
        probe if r.alone and r.text is not None else said(r)))
    report = run()
    assert run.probed() == [(RAISE, MINOR)]
    (kill,) = report["kills"]
    assert (kill["killer"], kill.get("via")) == (MINOR, None)


def test_only_the_killers_of_this_run_s_sites_are_gated(tree, monkeypatch):
    remember(tree, {RAISE: MINOR, BOOL: ADULT})
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    report = run(only=["BOOL"])
    assert run.gated() == [ADULT]
    assert "history:   1 remembered, 1 usable" in run.lines
    assert report["mutants"] == 1
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    run(only=["RAISE", "BOOL"], limit=1)
    assert len(run.gated()) == 1


def test_a_killer_shared_by_mutants_is_gated_once(tree, monkeypatch):
    remember(tree, {RAISE: MINOR, BOOL: MINOR})
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    run()
    assert run.gated() == [MINOR]
    assert "history:   2 remembered, 2 usable" in run.lines


@pytest.mark.parametrize("path", ["command line", "plugin"])
def test_a_killer_outside_the_selection_is_never_probed(tree, monkeypatch,
                                                       path):
    """A test the selection does not hold can kill a mutant the selection
    lets through: on the command line, the tests the first run kept; from
    pytest, the tests it collected."""
    remember(tree, {RAISE: MINOR, BOOL: ADULT})
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}),
                  selected=(MINOR,))
    if path == "plugin":
        run(selection=[MINOR])
    else:
        run()
    assert run.gated() == [MINOR]
    assert "history:   2 remembered, 1 usable" in run.lines


def test_tests_with_options_turn_the_probe_off(tree, monkeypatch):
    """A killer run alone leaves the options among the tests out, `-k`
    among them, so it could run a test they leave out."""
    remember(tree, {RAISE: ADULT})
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    report = run(tests=GATE_TESTS + ["-k", "minor"])
    assert run.gated() == [] and run.probed() == []
    assert "history:   not used: --tests has options" in run.lines
    assert "via" not in report["kills"][0]
    # What the whole selection measured is still kept.
    assert remembered(tree) == {RAISE: MINOR}


def test_with_nothing_remembered_there_is_no_gate_and_no_line(tree,
                                                             monkeypatch):
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    run()
    assert run.gated() == []
    assert not any(ln.startswith(("history:", "apart:")) for ln in run.lines)
    assert remembered(tree) == {RAISE: MINOR}


def test_a_mutant_not_run_keeps_its_killer_and_a_survivor_forgets_its(
        tree, monkeypatch):
    remember(tree, {RAISE: MINOR, BOOL: ADULT})
    run = Probing(tree, monkeypatch, world({}))
    run(only=["BOOL"])
    assert remembered(tree) == {RAISE: MINOR}


def test_a_site_gone_is_pruned(tree, monkeypatch):
    remember(tree, {RAISE: MINOR})
    with open(os.path.join(tree, ".invective", "history.json"), "r+",
              encoding="utf-8") as fh:
        data = json.load(fh)
        data[GATE_FILE]["gone"] = MINOR
        data["other.py"] = {"gone": MINOR}
        fh.seek(0)
        json.dump(data, fh)
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    run(only=["BOOL"])
    assert remembered(tree) == {RAISE: MINOR}
    assert remembered(tree, "other.py") == {"gone": MINOR}


@pytest.mark.parametrize("verdict", [
    mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT"),
    killed_by("pkg/tests/test_gate.py", ExitCode.INTERRUPTED)])
def test_a_timeout_or_module_killer_is_not_remembered(tree, monkeypatch,
                                                      verdict):
    remember(tree, {RAISE: MINOR})
    run = Probing(tree, monkeypatch, lambda r: verdict if r.text else GREEN)
    run(only=["RAISE"])
    assert remembered(tree) == {}


def test_history_false_writes_nothing_and_asks_for_no_inventory(
        tree, monkeypatch):
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    run(history=False)
    assert not os.path.exists(os.path.join(tree, ".invective"))
    assert not any(run.asked)


def test_with_history_off_no_run_asks_for_the_inventory(tree, monkeypatch):
    """A stand-in that takes no `inventory` keyword, as the engine's other
    tests have: with the history off, no run passes it."""
    run = Campaign(tree, monkeypatch, SOURCE, world({RAISE: [MINOR]}))
    report = run(2)
    assert report["killed"] == 1


def test_only_the_first_baseline_asks_for_the_inventory(tree, monkeypatch):
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    run(2)
    assert run.asked[0] and not any(run.asked[1:])


def test_a_run_lists_the_tests_it_kept_only_when_asked(tmp_path):
    """The inventory is the plugin's to write, and a run asks for it only
    when told to: no other run's verdict carries it."""
    (tmp_path / "test_two.py").write_text(
        "def test_a():\n    pass\n\ndef test_b():\n    pass\n",
        encoding="utf-8")
    plain = mutate.run_tests(str(tmp_path), ["test_two.py"], 60)
    asked = mutate.run_tests(str(tmp_path), ["test_two.py"], 60,
                             inventory=True)
    assert (plain.ok, plain.selected) == (True, ())
    assert (asked.ok, asked.selected) == (
        True, ("test_two.py::test_a", "test_two.py::test_b"))


def test_a_refused_run_creates_no_store(tree, monkeypatch):
    run = Probing(tree, monkeypatch, lambda r: killed_by(MINOR))
    with pytest.raises(mutate.Refusal):
        run()
    assert not os.path.exists(os.path.join(tree, ".invective"))


def test_an_interrupted_run_saves_what_landed(tree, monkeypatch):
    said = world({RAISE: [MINOR], "Lt -> LtE": [ADULT]})
    order = [site.what for site in _sites()]

    def stop_at_the_last(run):
        if run.text is not None and run.text == SITES[order[-1]].text:
            _thread.interrupt_main()
            assert run_.stop.wait(30)
            raise mutate.Stopped
        return said(run)

    run_ = Probing(tree, monkeypatch, stop_at_the_last)
    with pytest.raises(KeyboardInterrupt):
        run_()
    assert remembered(tree) == {RAISE: MINOR, "Lt -> LtE": ADULT}


def test_kills_that_end_as_the_campaign_stops_are_remembered(tree,
                                                            monkeypatch):
    """At three workers, a ^C as the first three mutants run: what their
    runs come to while the workers are stopped lands, and is kept."""
    interrupted = threading.Event()

    def said(run):
        if run.text is None:
            return GREEN
        if not interrupted.is_set():
            interrupted.set()
            _thread.interrupt_main()
        assert campaign.stop.wait(30)
        return (GREEN, killed_by(MINOR), killed_by(ADULT))[run.copy]

    campaign = Probing(tree, monkeypatch, said)
    with pytest.raises(KeyboardInterrupt):
        campaign(3)
    first = [site.what for site in _sites()][:3]
    assert remembered(tree) == {first[1]: MINOR, first[2]: ADULT}


def test_a_stop_during_a_probe_lands_nothing(tree, monkeypatch, landed):
    """A probe cut short is `Stopped`, never an outcome: neither the probe's
    verdict nor the whole selection's stands for its mutant."""
    remember(tree, {RAISE: MINOR})
    said = world({RAISE: [MINOR]})

    def stop_in_the_probe(run):
        if run.alone and run.text is not None:
            _thread.interrupt_main()
            assert campaign.stop.wait(30)
            raise mutate.Stopped
        return said(run)

    campaign = Probing(tree, monkeypatch, stop_in_the_probe)
    with pytest.raises(KeyboardInterrupt):
        campaign(2, only=["RAISE", "BOOL"])
    raise_n = next(n for n, (kind, what) in enumerate(
        [(s.kind, s.what) for s in _sites() if s.kind in ("RAISE", "BOOL")],
        1) if what == RAISE)
    assert raise_n not in [n for n, _o in landed]
    by_text = {site.text: site.what for site in SITES.values()}
    assert not [r for r, kind in zip(campaign.runs, campaign.files)
                if kind == "" and r.text and by_text[r.text] == RAISE]


def test_with_confirm_a_probe_kill_is_confirmed_like_any_other(
        tree, monkeypatch):
    remember(tree, {RAISE: MINOR})
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    report = run(2, confirm=True)
    (kill,) = report["kills"]
    assert (kill["via"], kill["confirmed"]) == ("probe", "alone")
    assert report["unreproduced"] == []
    assert "killer" in run.files


# --------------------------------------------------------------------------
# --no-unsafe-speedups


def test_without_unsafe_speedups_one_worker_runs_and_nothing_is_remembered(
        tree, monkeypatch):
    remember(tree, {RAISE: MINOR})
    before = remembered(tree)
    run = Probing(tree, monkeypatch, world({RAISE: [MINOR]}))
    report = run(3, unsafe_speedups=False)
    assert report["workers"] == 1 and len(run.places) == 1
    assert run.gated() == [] and run.probed() == [] and not any(run.asked)
    assert remembered(tree) == before
    assert "speedups:  safe ones only" in run.lines
    assert run.lines.index("speedups:  safe ones only") == (
        run.lines.index("tests:     %s" % " ".join(GATE_TESTS)) + 1)


def test_a_selection_red_beside_its_copies_is_refused_naming_the_switch(
        tree, monkeypatch):
    """The remedy for a suite not safe to run in parallel is the switch,
    which runs one mutant at a time against the whole selection."""
    run = Campaign(tree, monkeypatch, said=lambda r: (
        killed_by(MINOR) if r.copy == 1 else GREEN))
    with pytest.raises(mutate.Refusal) as caught:
        run(2)
    assert ("Run it with --no-unsafe-speedups, which runs one mutant at a "
            "time against the whole selection.") in str(caught.value)


@pytest.mark.parametrize("workers, history, said", [
    (2, True, "unsafe ones on: workers, history"),
    (2, False, "unsafe ones on: workers"),
    (1, True, "unsafe ones on: history"),
    (1, False, "no unsafe ones on"),
    # A count clamped to the one mutant runs one worker.
    (4, False, "no unsafe ones on")])
def test_the_header_says_which_unsafe_speedups_are_on(tree, monkeypatch,
                                                      workers, history, said):
    run = Probing(tree, monkeypatch, world({}))
    run(workers, only=["RAISE"] if workers == 4 else None, history=history)
    assert "speedups:  %s" % said in run.lines


# --------------------------------------------------------------------------
# Real runs


def _run(tree, *args):
    return subprocess.run(
        [sys.executable, "-m", "invective", "run", "--target", GATE,
         "--tests", *GATE_TESTS, "--json", "r.json", *args],
        cwd=tree, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": SRC})


def _verdicts(tree):
    with open(os.path.join(tree, "r.json"), encoding="utf-8") as fh:
        report = json.load(fh)
    return ({(k["line"], k["change"], k["killer"]) for k in report["kills"]},
            {(s["line"], s["change"]) for s in report["survivors"]}, report)


def test_a_second_real_run_has_the_same_verdicts(tree):
    first = _run(tree)
    assert first.returncode == 0, first.stderr
    kills, survivors, report = _verdicts(tree)
    assert not any("via" in k for k in report["kills"])
    second = _run(tree)
    assert second.returncode == 0, second.stderr
    again, survived, report = _verdicts(tree)
    assert (again, survived) == (kills, survivors)
    assert {k.get("via") for k in report["kills"]} == {"probe"}
    assert "1 of the kills were a remembered killer run alone" in second.stdout


def test_a_run_leaves_the_project_as_it_was_but_for_its_history(tree):
    """Every mutant is written in the copy, and the one thing a run writes
    in the project is `.invective/`."""
    def snapshot():
        found = {}
        for dirpath, _dirs, files in os.walk(tree):
            for name in files:
                full = os.path.join(dirpath, name)
                with open(full, "rb") as fh:
                    found[os.path.relpath(full, tree)] = fh.read()
        return found

    before = snapshot()
    done = subprocess.run(
        [sys.executable, "-m", "invective", "run", "--target", GATE,
         "--tests", *GATE_TESTS], cwd=tree, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": SRC})
    assert done.returncode == 0, done.stderr
    after = snapshot()
    added = {name for name in after if name not in before}
    assert added == {os.path.join(".invective", ".gitignore"),
                     os.path.join(".invective", "history.json")}
    assert {name: after[name] for name in before} == before
