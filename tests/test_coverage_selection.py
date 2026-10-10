"""Coverage-guided selection: each mutant run first against the tests that ran
its lines, and decided by the whole selection unless one of them kills it.

Most of these drive the engine with a stand-in for `run_tests`, as
`test_workers` does, that also records the coverage run's map and runs a
narrower selection; the rest run real campaigns.
"""

from __future__ import annotations

import _thread
import importlib.metadata
import importlib.util
import json
import os
import threading
import time
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from pytest import ExitCode

import pytest_invective
from invective import mutate

from conftest import FILES, write_tree
from test_covers import write_map
from test_workers import (ADULT, GATE, GATE_TESTS, GREEN, MINOR, Campaign,
                          by_text, killed_by)
# A fixture: imported, pytest finds it here.
from test_workers import landed  # noqa: F401


class Run(NamedTuple):
    """One run, as the stand-in saw it."""

    #: The copy, by the order its `copy:` line was said: 0 is the first.
    copy: int
    #: The target's text in the copy, None when it is the original.
    text: str | None
    #: `selection`, `coverage`, `narrowed` (a file of `narrowed-`), `probe`
    #: (a remembered killer's file) or `alone` (confirmation's file of one
    #: killer).
    kind: str
    #: The tests a file named, `()` for the selection.
    tests: tuple[str, ...]
    timeout: float


class Covered(Campaign):
    """`test_workers.Campaign`, whose coverage run records *lines*, each line
    of `pkg/gate.py` to the contexts it ran under, for the selection
    *tests*; each run is what `said(run)` says, green unless it says
    otherwise."""

    def __init__(self, tree, monkeypatch, lines=None, said=None,
                 tests=(MINOR, ADULT), **kwargs):
        super().__init__(tree, monkeypatch, said=said, **kwargs)
        self.map, self.tests = lines or {}, tests

    def run_tests(self, where, tests, timeout, selection=None, options=(),
                  target="", stop=None, *, inventory=False, coverage=None):
        self.stop = stop
        with open(os.path.join(where, GATE), encoding="utf-8",
                  newline="") as fh:
            text = fh.read()
        kind, named = "selection", ()
        if coverage is not None:
            kind = "coverage"
            write_map(coverage, os.path.realpath(os.path.join(where, GATE)),
                      self.map, self.tests)
        elif selection is not None and os.path.basename(selection) != (
                "selection.txt"):
            kind = {"narrowed": "narrowed", "probe": "probe"}.get(
                os.path.basename(selection).partition("-")[0], "alone")
            with open(selection, encoding="utf-8") as fh:
                named = tuple(fh.read().splitlines())
        run = Run(self.places.index(where),
                  None if text == self.source else text, kind, named, timeout)
        with self.lock:
            self.runs.append(run)
        got = self.said(run)
        # The tests the run kept, which a remembered killer is looked for in.
        return got._replace(selected=self.tests) if inventory else got

    def of(self, kind):
        return [run for run in self.runs if run.kind == kind]


#: The refusal on line 3 is run by the refusal's test alone.
REFUSAL = {2: ["0", "1"], 3: ["0"]}


def test_a_mutant_the_covering_tests_kill_is_their_kill(tree, monkeypatch):
    """The refusal's mutant is run against the one test that ran its line,
    at the mutants' whole budget, and its kill stands, marked as theirs; no
    narrower selection is run on the original first."""
    run = Covered(tree, monkeypatch, REFUSAL, said=lambda r: (
        killed_by(MINOR) if r.text and "pass" in r.text else GREEN))
    report = run(1, coverage=True)

    (kill,) = report["kills"]
    assert (kill["line"], kill["killer"], kill["via"]) == (3, MINOR,
                                                            "coverage")
    assert report["coverage"] == {"narrowed": 1, "unused": ""}
    # The selection and the coverage run on the original, then each mutant
    # once: the refusal's against its test alone.
    assert [(r.kind, r.text is None) for r in run.runs[:3]] == [
        ("selection", True), ("coverage", True), ("selection", False)]
    (narrowed,) = run.of("narrowed")
    assert "pass" in narrowed.text and narrowed.tests == (MINOR,)
    assert narrowed.timeout == next(r.timeout for r in run.of("selection")
                                    if r.text)
    assert len(run.of("selection")) == 1 + report["mutants"] - 1
    i = run.lines.index("coverage:  1/6 mutants narrowed (1 selection(s))")
    assert run.lines[i - 1].startswith("baseline:")
    assert run.lines[i + 1] == "mutants:   6\n"
    assert mutate.summary(report)[1:] == [
        "           1 of the kills were the covering tests run alone"]


def test_a_mutant_the_covering_tests_let_through_is_the_selection_s(
        tree, monkeypatch, landed):
    """Its narrower run passes, and the whole selection, run next, kills
    it with a test the narrower one did not have: a kill, and no mark."""
    def said(run):
        if run.text and "pass" in run.text and run.kind == "selection":
            return killed_by(ADULT)
        return GREEN

    run = Covered(tree, monkeypatch, REFUSAL, said=said)
    report = run(1, only=["RAISE"], coverage=True)

    assert report["kills"] == [{**report["kills"][0], "killer": ADULT}]
    assert "via" not in report["kills"][0]
    assert [r.kind for r in run.runs if r.text] == ["narrowed", "selection"]
    assert [o.via for _n, o in landed] == [""]


def test_a_mutant_that_hangs_the_covering_tests_is_a_kill_by_time_at_once(
        tree, monkeypatch, landed):
    """The covering tests run out the mutants' whole budget, which the
    whole selection, holding them, would run out too: a kill by time,
    marked, for one budget and not two."""
    timed_out = mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT")
    run = Covered(tree, monkeypatch, REFUSAL, said=lambda r: (
        timed_out if r.text and "pass" in r.text else GREEN))
    report = run(1, only=["RAISE"], coverage=True)

    (kill,) = report["kills"]
    assert (kill["killer"], kill["code"], kill["via"]) == (
        "TIMEOUT", mutate.TIMED_OUT, "coverage")
    (narrowed,) = [r for r in run.runs if r.text]
    assert narrowed.kind == "narrowed"
    # The mutants' budget: three times a baseline that took no time, and at
    # least 30 s.
    assert narrowed.timeout == 30.0
    assert ("           1 of the kills were runs stopped at their time "
            "budget, not a test failing") in mutate.summary(report)
    assert [o.via for _n, o in landed] == ["coverage"]


def test_a_mutant_that_slows_the_covering_tests_inside_the_budget_is_not(
        tree, monkeypatch):
    """Slower, but done inside the mutants' budget: their pass is no
    verdict, and the whole selection decides the mutant, here a
    survivor."""
    run = Covered(tree, monkeypatch, REFUSAL, said=lambda r: GREEN._replace(
        tail="slow") if r.text else GREEN)
    report = run(1, only=["RAISE"], coverage=True)

    assert report["kills"] == [] and len(report["survivors"]) == 1
    assert [(r.kind, r.timeout) for r in run.runs if r.text] == [
        ("narrowed", 30.0), ("selection", 30.0)]


def test_no_narrower_selection_is_run_on_the_original(tree, monkeypatch):
    """Under the contract any part of the green selection is green: a
    narrower selection red on the original is not looked for, and a kill
    by one of its tests stands."""
    run = Covered(tree, monkeypatch, REFUSAL, said=lambda r: (
        killed_by(MINOR) if r.kind == "narrowed" else GREEN))
    report = run(1, coverage=True)

    assert [r.text is None for r in run.of("narrowed")] == [False]
    assert [(k["line"], k["via"]) for k in report["kills"]] == [
        (3, "coverage")]


def test_mutants_of_lines_the_same_tests_ran_share_one_selection(
        tree, monkeypatch):
    """Line 2's two mutants are each the refusal's test's, and line 4's three
    the adult's: a file for each."""
    run = Covered(tree, monkeypatch, {2: ["0"], 4: ["1"]})
    report = run(1, only=["CMP", "CONST", "BOOL"], coverage=True)
    assert report["coverage"]["narrowed"] == 5
    assert "coverage:  5/5 mutants narrowed (2 selection(s))" in run.lines
    files = {}
    for r in run.of("narrowed"):
        files.setdefault(r.tests, set()).add(r.text)
    assert {tests: len(texts) for tests, texts in files.items()} == {
        (MINOR,): 2, (ADULT,): 3}


def _written(monkeypatch):
    """Each text written as a copy's target, in order."""
    texts = []
    real = mutate._write
    monkeypatch.setattr(mutate, "_write", lambda path, text, when: (
        texts.append(text), real(path, text, when)))
    return texts


@pytest.mark.parametrize("narrowed, survives", [(killed_by(MINOR), False),
                                                (GREEN, True)])
def test_a_probe_that_says_nothing_leaves_the_mutant_to_the_narrowed_run(
        tree, monkeypatch, narrowed, survives):
    """The refusal's mutant has a remembered killer, the adult's test, which
    passes alone on it; the covering test is run next, and kills it, marked
    as the covering tests' kill, or lets it through to the whole selection,
    which decides a survivor. The mutant is written once for all three."""
    from test_probe import RAISE, SITES, remember

    remember(tree, {RAISE: ADULT})
    texts = _written(monkeypatch)

    def said(run):
        if run.text is None or "pass" not in run.text:
            return GREEN
        return narrowed if run.kind == "narrowed" else GREEN

    run = Covered(tree, monkeypatch, REFUSAL, said=said)
    report = run(1, only=["RAISE"], history=True, coverage=True)

    on_mutant = [(r.kind, r.tests) for r in run.runs if r.text]
    if survives:
        assert report["kills"] == [] and len(report["survivors"]) == 1
        assert on_mutant == [("probe", (ADULT,)), ("narrowed", (MINOR,)),
                             ("selection", ())]
    else:
        assert [(k["killer"], k["via"]) for k in report["kills"]] == [
            (MINOR, "coverage")]
        assert on_mutant == [("probe", (ADULT,)), ("narrowed", (MINOR,))]
    assert texts.count(SITES[RAISE].text) == 1
    i = next(n for n, line in enumerate(run.lines)
             if line.startswith("history:"))
    assert run.lines[i + 1].startswith("coverage:")


def test_a_probe_s_kill_leaves_no_narrowed_run(tree, monkeypatch):
    """The remembered killer fails alone on the mutant: that is its kill,
    and the covering tests are not run on it."""
    from test_probe import RAISE, remember

    remember(tree, {RAISE: MINOR})
    run = Covered(tree, monkeypatch, REFUSAL, said=lambda r: (
        killed_by(MINOR) if r.text and "pass" in r.text else GREEN))
    report = run(1, only=["RAISE"], history=True, coverage=True)

    assert [(k["killer"], k["via"]) for k in report["kills"]] == [
        (MINOR, "probe")]
    assert [(r.kind, r.tests) for r in run.runs if r.text] == [
        ("probe", (MINOR,))]


@pytest.mark.parametrize("narrowed, kept", [
    (killed_by(MINOR), MINOR),
    (mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT"), None)])
def test_a_narrowed_kill_is_remembered_as_any_kill_is(tree, monkeypatch,
                                                      narrowed, kept):
    """The remembered killer passes alone on the mutant and the covering
    test kills it: the history keeps that test in its place. A kill by
    time names no test to run alone, so the old killer is forgotten and
    nothing takes its place."""
    from test_probe import RAISE, remember, remembered

    remember(tree, {RAISE: ADULT})
    run = Covered(tree, monkeypatch, REFUSAL, said=lambda r: (
        narrowed if r.text and "pass" in r.text and r.kind == "narrowed"
        else GREEN))
    report = run(1, only=["RAISE"], history=True, coverage=True)

    assert [k["via"] for k in report["kills"]] == ["coverage"]
    assert remembered(tree).get(RAISE) == kept


def test_no_unsafe_speedups_turns_coverage_off(tree, monkeypatch):
    """Asked for coverage with the unsafe speedups off, a direct call runs
    no coverage run and no narrowed run, and says only the safe ones run;
    the command takes `coverage = true` in the settings as off with the
    switch."""
    run = Covered(tree, monkeypatch, REFUSAL, said=lambda r: (
        killed_by(MINOR) if r.text and "pass" in r.text else GREEN))
    report = run(1, coverage=True, unsafe_speedups=False)
    assert {r.kind for r in run.runs} == {"selection"}
    assert "coverage" not in report
    assert "speedups:  safe ones only" in run.lines
    assert not any(line.startswith("coverage:") for line in run.lines)

    given = []
    monkeypatch.setattr(mutate, "mutate", lambda *a, **k: given.append(
        k["coverage"]) or {
            "killed": 0, "mutants": 1, "survivors": [], "accepted": [],
            "stale": [], "kills": [], "broken": 0})
    write_tree(tree, {"pyproject.toml": (
        "[tool.pytest.ini_options]\n[tool.invective]\ncoverage = true\n")})
    monkeypatch.chdir(tree)
    args = ["--target", GATE, "--tests", "pkg/tests/test_gate.py"]
    mutate.main(args + ["--no-unsafe-speedups"])
    mutate.main(args)
    assert given == [False, True]


def test_the_speedups_line_names_coverage_when_it_runs(tree, monkeypatch):
    run = Covered(tree, monkeypatch, REFUSAL)
    run(1, coverage=True)
    assert "speedups:  unsafe ones on: coverage" in run.lines


def test_with_coverage_off_no_run_is_added(tree, monkeypatch):
    run = Covered(tree, monkeypatch, REFUSAL)
    report = run(1)
    assert "coverage" not in report
    assert [r.kind for r in run.runs] == ["selection"] * (1 + 6)
    assert not any(line.startswith("coverage:") for line in run.lines)
    assert all(not line.startswith("coverage:")
               for line in mutate.summary(report))


@pytest.mark.parametrize("workers", [1, 3])
def test_the_report_is_the_same_with_coverage_on_but_for_how(
        tree, monkeypatch, workers):
    """Every verdict, killer and entry with coverage on is the one it is
    off, but for the mark on the kills the covering tests made and the
    `coverage` key; the stand-in's tests fail on a mutant whichever of
    them run, as tests whose order does not matter do."""
    def said(run):
        got = by_text(run)
        # A test not among the narrower ones does not fail there; a mutant
        # that hangs, hangs there too.
        if (run.kind == "narrowed" and got.killer not in run.tests
                and got.code != mutate.TIMED_OUT):
            return GREEN
        return got

    # Every line is run by the fixture's two tests, half of four.
    lines = {n: ["0", "1"] for n in range(1, 7)}
    tests = (MINOR, ADULT, "pkg/tests/test_gate.py::test_x",
             "pkg/tests/test_gate.py::test_y")
    off = Covered(tree, monkeypatch, lines, said=said, tests=tests)(1)
    run = Covered(tree, monkeypatch, lines, said=said, tests=tests)
    on = run(workers, coverage=True)

    assert any(kill.get("via") == "coverage" for kill in on["kills"])
    assert on.pop("coverage")["narrowed"] > 0
    stripped = {**on, "workers": 1, "kills": [
        {key: value for key, value in kill.items() if key != "via"}
        for kill in on["kills"]]}
    assert stripped == off


# --------------------------------------------------------------------------
# When coverage is not used


def _version(given):
    def version(name):
        assert name == "coverage"
        if given is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return given
    return version


@pytest.mark.parametrize("version, unused", [
    (None, "coverage is not installed"),
    ("7.12.9", "coverage 7.12.9 is older than 7.13"),
    ("6.99", "coverage 6.99 is older than 7.13"),
    ("7.13.0", ""), ("8.0", ""), ("7.16.2", "")])
def test_coverage_this_python_cannot_record_with_is_said(version, unused,
                                                         monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", _version(version))
    assert mutate._coverage_unavailable() == unused


@pytest.mark.parametrize("case", [
    "missing", "old", "red", "capped", "unread", "options", "comma",
    "dollar"])
def test_coverage_that_cannot_be_used_is_said_and_every_mutant_runs_whole(
        tree, monkeypatch, tmp_path, case):
    """Not installed, too old, a coverage run that failed or ran out of
    time, a map that cannot be read, `--tests` holding an option, or a path
    coverage's settings cannot hold: the header and the closing lines say
    why, and the campaign is what it is with coverage off."""
    tests = GATE_TESTS
    said = GREEN
    if case == "missing":
        monkeypatch.setattr(importlib.metadata, "version", _version(None))
        why = "coverage is not installed"
    elif case == "old":
        monkeypatch.setattr(importlib.metadata, "version", _version("7.12.0"))
        why = "coverage 7.12.0 is older than 7.13"
    elif case == "red":
        said = killed_by(MINOR)
        why = "the coverage run exited 1"
    elif case == "capped":
        said = mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT")
        why = "the coverage run took longer than 30 s"
    elif case == "unread":
        said = GREEN._replace(tail="no map")
        why = "the coverage run's map could not be read: "
    elif case == "options":
        tests = GATE_TESTS + ["-k", "minor or adult"]
        why = ("--tests holds an option, which a narrower selection would "
               "leave out")
    else:
        odd = tmp_path / ("a,b" if case == "comma" else "a$b")
        odd.mkdir()
        monkeypatch.setattr(mutate.tempfile, "tempdir", str(odd))
        why = "the target's path holds a `,` or `$`"

    class Unmapped(Covered):
        def run_tests(self, where, tests, timeout, selection=None,
                      options=(), target="", stop=None, *, inventory=False,
                      coverage=None):
            if coverage is not None:
                self.runs.append(Run(0, None, "coverage", (), timeout))
                return said
            return super().run_tests(where, tests, timeout, selection,
                                     options, target, stop)

    run = (Unmapped if case in ("red", "capped", "unread") else Covered)(
        tree, monkeypatch, REFUSAL, said=lambda r: (
            killed_by(MINOR) if r.text and "pass" in r.text else GREEN))
    report = run(1, coverage=True, tests=tests)

    line = "coverage:  not used: %s" % why
    (said_line,) = [ln for ln in run.lines if ln.startswith("coverage:")]
    assert said_line.startswith(line)
    assert said_line.endswith("; every mutant runs the full selection")
    assert report["coverage"]["narrowed"] == 0
    assert report["coverage"]["unused"].startswith(why)
    assert mutate.summary(report)[-1] == said_line
    assert report["kills"][0].get("via") is None
    assert run.of("narrowed") == []
    ran = [r.kind for r in run.runs if r.kind == "coverage"]
    assert ran == (["coverage"] if case in ("red", "capped", "unread")
                   else [])


def test_the_coverage_run_is_cut_at_ten_times_the_baseline(tree,
                                                           monkeypatch):
    """Past that, its map would cost more than it saves; a narrower
    selection's run is cut where the whole selection's is, at the mutants'
    budget."""
    now = [1_000_000.0]
    monkeypatch.setattr(mutate, "time", SimpleNamespace(
        time=lambda: now[0], monotonic=time.monotonic))

    def said(run):
        if run.kind == "selection" and run.text is None:
            now[0] += 20
        return GREEN

    run = Covered(tree, monkeypatch, REFUSAL, said=said)
    run(1, only=["RAISE"], coverage=True)
    (coverage,) = run.of("coverage")
    assert coverage.timeout == 200
    # The mutants' budget, whatever multiple of the baseline it is: its
    # acceptance says any well above the baseline serves.
    (narrowed,) = run.of("narrowed")
    assert narrowed.timeout == next(r.timeout for r in run.of("selection")
                                    if r.text)


class _Copy:
    """A copy whose every run says *verdict*, and that keeps each run's
    timeout and selection."""

    def __init__(self, verdict):
        self.verdict, self.runs = verdict, []

    def run(self, mutant, timeout, selection=None, **handshake):
        self.runs.append((timeout, selection))
        return self.verdict


def _mutant(idx=0):
    return mutate.Mutant(mutate.Job(1, idx, "RAISE", "raise ... -> pass", 3),
                         "text", True)


PLAN = {0: mutate.Narrowed("narrowed-0.txt", frozenset({MINOR}))}


@pytest.mark.parametrize("got", [
    killed_by("pkg/tests/test_gate.py", ExitCode.INTERRUPTED),
    killed_by("", ExitCode.USAGE_ERROR),
    killed_by("", ExitCode.NO_TESTS_COLLECTED),
    killed_by(ADULT),
    killed_by("pkg/tests/test_gate.py"),
    killed_by(MINOR)._replace(missing=(MINOR,)),
    GREEN])
def test_only_its_own_failing_test_makes_a_narrowed_kill(got):
    """A module that would not import, a run that collected nothing, a test
    not among them, a test missing, or a pass: the mutant is the whole
    selection's to decide. The run is at the whole budget."""
    copy = _Copy(got)
    assert mutate.coverage_attempt(PLAN, 30)(_mutant(), copy) is None
    assert copy.runs == [(30, "narrowed-0.txt")]


@pytest.mark.parametrize("got", [
    killed_by(MINOR),
    mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT")])
def test_a_failing_test_of_the_narrower_selection_or_time_is_its_kill(got):
    copy = _Copy(got)
    assert mutate.coverage_attempt(PLAN, 30)(_mutant(), copy) == (
        mutate.Outcome(got, via="coverage"))


def test_a_mutant_with_no_narrower_selection_is_not_run_by_the_attempt():
    copy = _Copy(killed_by(MINOR))
    assert mutate.coverage_attempt(PLAN, 30)(_mutant(1), copy) is None
    assert copy.runs == []


def test_a_stop_during_a_narrowed_run_lands_nothing(tree, monkeypatch,
                                                     landed):
    """The narrowed run is cut short by a ^C and is no verdict: it is not
    taken for a pass and handed to the whole selection, which would land a
    survivor; the other mutant's run, ended, lands."""
    interrupted = threading.Event()

    def said(run):
        if run.text is None:
            return GREEN
        if run.kind == "narrowed":
            interrupted.set()
            _thread.interrupt_main()
            assert campaign.stop.wait(30)
            raise mutate.Stopped
        assert interrupted.wait(30) and campaign.stop.wait(30)
        return GREEN

    campaign = Covered(tree, monkeypatch, REFUSAL, said=said)
    with pytest.raises(KeyboardInterrupt):
        campaign(2, only=["RAISE", "BOOL"], coverage=True)
    (refusal,) = [r for r in campaign.runs if r.kind == "narrowed" and r.text]
    assert [o.verdict.ok for _n, o in landed] == [True]
    assert not [r for r in campaign.runs
                if r.text == refusal.text and r.kind == "selection"]


@pytest.mark.parametrize("alone, confirmed", [
    (killed_by(MINOR), "alone"), (GREEN, "full")])
def test_with_confirm_a_narrowed_kill_is_confirmed_as_any_other(
        tree, monkeypatch, alone, confirmed):
    """At one worker the kill stands, marked; at two, asked to confirm, its
    killer alone confirms it and the mark stays, or else the whole
    selection decides, and a kill its killer does not make alone is
    named."""
    def said(run):
        if run.text is None or "pass" not in run.text:
            return GREEN
        return {"narrowed": killed_by(MINOR), "alone": alone,
                "selection": GREEN}[run.kind]

    one = Covered(tree, monkeypatch, REFUSAL, said=said)(
        1, only=["RAISE", "BOOL"], coverage=True)
    assert [(k["killer"], k["via"]) for k in one["kills"]] == [
        (MINOR, "coverage")]

    two = Covered(tree, monkeypatch, REFUSAL, said=said)(
        2, only=["RAISE", "BOOL"], coverage=True, confirm=True)
    if confirmed == "alone":
        assert [(k["via"], k["confirmed"]) for k in two["kills"]] == [
            ("coverage", "alone")]
        assert two["unreproduced"] == []
    else:
        assert two["kills"] == []
        assert sorted(s.get("confirmed", "") for s in two["survivors"]) == [
            "", "full"]
        assert [(u["line"], u["killer"], u["alone"], u["again"])
                for u in two["unreproduced"]] == [
            (3, MINOR, "passes alone on the mutant", "survived")]


# --------------------------------------------------------------------------
# The coverage run's own run


class _Started(Exception):
    pass


def _started(monkeypatch):
    """Each run's command line and environment, kept as `Popen` is asked to
    start it, and the run then ended there."""
    seen = []

    def popen(argv, **kwargs):
        seen.append((argv, kwargs["env"]))
        raise _Started

    monkeypatch.setattr(mutate.subprocess, "Popen", popen)
    return seen


@pytest.mark.parametrize("coverage", [None, "/box"])
def test_the_coverage_run_alone_goes_without_the_user_s_coverage(
        monkeypatch, coverage):
    """Coverage's own variables, and pytest-cov's, would record the
    coverage run's children elsewhere or twice; every other run is given
    them as the user has them."""
    seen = _started(monkeypatch)
    for name in ("COVERAGE_FILE", "COVERAGE_PROCESS_START",
                 "COV_CORE_SOURCE", "COVERAGES"):
        monkeypatch.setenv(name, "theirs")
    with pytest.raises(_Started):
        mutate.run_tests("/nowhere", ["t.py"], 1, coverage=coverage)
    ((_argv, env),) = seen
    kept = {name for name in env if name.startswith("COV")}
    if coverage is None:
        assert kept >= {"COVERAGE_FILE", "COVERAGE_PROCESS_START",
                        "COV_CORE_SOURCE", "COVERAGES"}
        assert pytest_invective.COVERAGE not in env
    else:
        assert kept == {"COVERAGES"}
        assert env[pytest_invective.COVERAGE] == "/box"


@pytest.mark.parametrize("installed, autoload, options, given", [
    (True, "", (), True),
    (True, "", ("-p", "x", "-W", "error"), True),
    (False, "", (), False),
    (True, "1", (), False),
    (True, "", ("-p", "no:cov"), False),
    (True, "", ("-pno:cov",), False),
    (True, "", ("-p", "no:pytest_cov"), False)])
def test_no_cov_follows_the_options_only_when_pytest_cov_loads(
        monkeypatch, installed, autoload, options, given):
    """pytest-cov's recorder, started by a `--cov` in the project's
    `addopts`, would record over the coverage run's; where pytest-cov is
    not loaded, `--no-cov` is an option pytest does not know."""
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: (
        (SimpleNamespace() if installed else None) if name == "pytest_cov"
        else real(name)))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", autoload)
    seen = _started(monkeypatch)
    for coverage in (None, "/box"):
        with pytest.raises(_Started):
            mutate.run_tests("/nowhere", ["t.py"], 1, options=options,
                             coverage=coverage)
    (plain, _env), (covered, _env) = seen
    assert "--no-cov" not in plain
    at = covered.index("-x")
    assert covered[at - len(options) - given:at] == list(options) + [
        "--no-cov"] * given


# --------------------------------------------------------------------------
# Real campaigns


@pytest.mark.parametrize("workers", [1, 4])
def test_the_verdicts_are_the_same_with_coverage_on_and_off(tree, workers):
    """The fixture's refusal is run by its one test alone, which kills it;
    every other mutant goes to the whole selection, and no verdict moves."""
    def sets(report):
        return [sorted((e["kind"], e["line"], e["change"]) for e in
                       report[key]) for key in ("kills", "survivors")]

    def run(coverage):
        return mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS,
                             None, None, say=lambda line: None,
                             workers=workers, coverage=coverage)

    off, on = run(False), run(True)
    assert on["workers"] == workers
    assert [(k["line"], k["killer"]) for k in on["kills"]
            if k.get("via") == "coverage"] == [(3, MINOR)]
    assert on["coverage"]["narrowed"] >= 1
    assert sets(on) == sets(off)
    assert [k["killer"] for k in on["kills"]] == [
        k["killer"] for k in off["kills"]]


#: A test that needs another's state: `test_k` reads what `test_a` left,
#: and `test_x`, between them, clears it, as the whole selection's order
#: has them. `test_a` and `test_k` alone run the comparison of line 2.
ORDERED = {
    "pkg/flag.py": ("def admit(count):\n"
                    "    if count > 1:\n"
                    "        raise ValueError('twice')\n"
                    "    return count\n"),
    "pkg/tests/test_flag.py": ("import pytest\n"
                               "from pkg import flag\n"
                               "\n"
                               "SEEN = []\n"
                               "\n"
                               "def test_a():\n"
                               "    SEEN.append(1)\n"
                               "    with pytest.raises(ValueError):\n"
                               "        flag.admit(5)\n"
                               "\n"
                               "def test_x():\n"
                               "    SEEN.clear()\n"
                               "\n"
                               "def test_k():\n"
                               "    assert flag.admit(len(SEEN)) == len(SEEN)\n"
                               "\n"
                               "def test_y():\n"
                               "    pass\n"),
}


def test_a_suite_whose_tests_need_their_order_is_named_by_confirm(tree):
    """Outside the contract: the comparison's mutant fails `test_k` after
    `test_a` alone, passes `test_k` alone, and passes the whole selection,
    whose `test_x` clears what `test_a` left. One worker reports the
    covering tests' kill, marked; two, asked to confirm, find the killer
    does not make it alone, and the whole selection lets it through."""
    write_tree(tree, ORDERED)

    def run(workers, confirm):
        return mutate.mutate(tree, os.path.join(tree, "pkg", "flag.py"),
                             ["pkg/tests/test_flag.py"], ["CMP", "CONST"],
                             None, say=lambda line: None, workers=workers,
                             confirm=confirm, coverage=True)

    k = "pkg/tests/test_flag.py::test_k"
    one = run(1, False)
    assert [(e["change"], e["killer"], e["via"]) for e in one["kills"]] == [
        ("Gt -> GtE", k, "coverage")]
    assert one["coverage"] == {"narrowed": 2, "unused": ""}

    two = run(2, True)
    assert two["kills"] == []
    assert [(u["change"], u["killer"], u["alone"], u["again"])
            for u in two["unreproduced"]] == [
        ("Gt -> GtE", k, "passes alone on the mutant", "survived")]


@pytest.mark.parametrize("setting, coverage", [("", False),
                                               ("coverage = true\n", True)])
def test_the_command_takes_coverage_from_the_settings(tree, monkeypatch,
                                                      setting, coverage):
    write_tree(tree, {"pyproject.toml": (
        "[tool.pytest.ini_options]\n[tool.invective]\n" + setting)})
    given = []
    monkeypatch.setattr(mutate, "mutate", lambda *a, **k: given.append(
        k["coverage"]) or {
            "killed": 0, "mutants": 1, "survivors": [], "accepted": [],
            "stale": [], "kills": [], "broken": 0})
    monkeypatch.chdir(tree)
    mutate.main(["--target", GATE, "--tests", "pkg/tests/test_gate.py"])
    assert given == [coverage]


# --------------------------------------------------------------------------
# The closing lines


def _report(**given):
    return {"target": GATE, "mutants": 4, "killed": 3, "survivors": [{}],
            "accepted": [], "stale": [], "broken": 0, **given}


def test_the_closing_lines_count_the_covering_tests_kills():
    kills = [{"code": 1, "killer": MINOR, "via": "coverage"},
             {"code": 1, "killer": MINOR, "origin": "coverage"},
             {"code": 1, "killer": MINOR, "via": "probe"}]
    report = _report(kills=kills, coverage={"narrowed": 2, "unused": ""},
                     unreproduced=[{"line": 3, "change": "x", "killer": MINOR,
                                    "alone": "passes alone on the mutant",
                                    "again": "killed"}])
    lines = mutate.summary(report)
    # The probe's line, then coverage's, then the kills to look at.
    assert lines[1] == ("           1 of the kills were a remembered killer "
                        "run alone")
    assert lines[2] == ("           2 of the kills were the covering tests "
                        "run alone")
    assert lines[3].startswith("           1 kill(s) did not come back")
    assert not any("not used" in line for line in lines)
