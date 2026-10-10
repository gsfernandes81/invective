"""The verdict cache: each verdict kept as it lands, and read back instead of
a run by a later run of the same campaign that could have decided it, so
that a stopped run resumes where it stopped.

Most of these drive the engine with `Caching`, `test_probe.Probing` with the
cache on and the history off unless a test turns it on, whose first baseline
also says which files it imported; the coverage ones with `Covering`,
`test_coverage_selection.Covered` likewise. `(SD)` in a docstring marks a
check of the silent direction: a verdict read back where a run under this
run's settings could have decided otherwise.
"""

from __future__ import annotations

import _thread
import collections
import glob
import json
import os
import shutil
import subprocess
import sys

import pytest
from pytest import ExitCode

from invective import mutate, store

from conftest import SRC, commit, write_tree
from test_coverage_selection import REFUSAL, Covered
from test_probe import (BOOL, RAISE, SITES, SOURCE, Probing, _sites, remember,
                        world)
from test_workers import (ADULT, GATE, GATE_TESTS, GREEN, MINOR, TARGETS,
                          by_text, killed_by)

#: The changes of `SOURCE`'s mutants, in the order they are run.
ORDER = [site.what for site in _sites()]
#: Each mutant's change, by its text.
BY_TEXT = {site.text: site.what for site in SITES.values()}
#: What the first baseline says it imported.
IMPORTED = ("pkg/__init__.py",)
#: A kill by time.
TIMEOUT = mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT")


class Caching(Probing):
    """`Probing`, with the verdict cache on and the history off unless asked,
    on *source* as `pkg/gate.py`; its first baseline also says it imported
    *imported*."""

    def __init__(self, tree, monkeypatch, said=None, source=SOURCE,
                 imported=IMPORTED, **kwargs):
        super().__init__(tree, monkeypatch, said, **kwargs)
        if source != SOURCE:
            self.source = source
            write_tree(tree, {"pkg/gate.py": source})
        self.imported = imported

    def run_tests(self, *args, inventory=False, **kwargs):
        got = super().run_tests(*args, inventory=inventory, **kwargs)
        return got._replace(imported=self.imported) if inventory else got

    def __call__(self, workers=1, only=None, limit=None, **kwargs):
        kwargs.setdefault("cache", True)
        kwargs.setdefault("history", False)
        return super().__call__(workers, only, limit, **kwargs)

    def ran(self):
        """The changes of the mutants a run was made on, in order, whatever
        the run."""
        return [BY_TEXT.get(r.text, r.text) for r in self.runs
                if r.text is not None]


class Covering(Covered):
    """`Covered`, whose first baseline also says what it imported."""

    def run_tests(self, *args, inventory=False, **kwargs):
        got = super().run_tests(*args, inventory=inventory, **kwargs)
        return got._replace(imported=IMPORTED) if inventory else got

    def ran(self):
        return [(BY_TEXT[r.text], r.kind) for r in self.runs
                if r.text is not None]


def kept(tree):
    """Every entry the cache holds, of any campaign, by its mutant's
    change."""
    by_mutant = {store._hex(site.text): what for what, site in SITES.items()}
    found = {}
    for path in glob.glob(os.path.join(tree, ".invective", "cache", "*", "*",
                                       "*.json")):
        with open(path, encoding="utf-8") as fh:
            entry = json.load(fh)
        found[by_mutant.get(entry["mutant"], entry["mutant"])] = entry
    return found


def forget(tree):
    """No verdict kept."""
    shutil.rmtree(os.path.join(tree, ".invective", "cache"),
                  ignore_errors=True)


def _without_via(report):
    return {key: ([{k: v for k, v in e.items() if k != "via"} for e in value]
                  if key in ("kills", "survivors", "accepted") else value)
            for key, value in report.items()}


# --------------------------------------------------------------------------
# The core


def test_a_cached_verdict_is_not_run_again_and_is_marked(tree, monkeypatch):
    """The second run runs no mutant: each entry carries `"via": "cache"`,
    the survivors' and the accepted's lines say `[cache]`, the closing lines
    count them, and the header says how many verdicts the campaign kept."""
    source = SOURCE.replace(
        "    if member and age >= 65:\n",
        "    if member and age >= 65:  # invective: accept[untestable] old\n")

    def said(run):
        return (killed_by(MINOR) if run.text is not None
                and "raise" not in run.text else GREEN)

    first = Caching(tree, monkeypatch, said, source=source)
    cold = first()
    assert len(first.ran()) == 6
    assert "cache:     0 on record" in first.lines
    assert not any("[cache]" in line for line in first.lines)
    assert not any("via" in e for key in ("kills", "survivors", "accepted")
                   for e in cold[key])
    assert len(kept(tree)) == 6

    second = Caching(tree, monkeypatch, said, source=source)
    warm = second()
    assert second.ran() == []
    i = second.lines.index("cache:     6 on record")
    assert second.lines[i + 1] == "mutants:   6\n"
    assert {e["via"] for key in ("kills", "survivors", "accepted")
            for e in warm[key]} == {"cache"}
    assert not any("origin" in e or "confirmed" in e
                   for e in warm["kills"] + warm["survivors"])
    assert len(warm["survivors"]) == 2 and len(warm["accepted"]) == 3
    marked = [ln for ln in second.lines
              if ln.startswith(("  SURVIVED", "  accepted"))]
    assert len(marked) == 5 and all(ln.endswith(" [cache]") for ln in marked)
    assert ("           6 of the verdicts were read from .invective/cache, "
            "not run") in mutate.summary(warm)
    assert not any("read from" in line for line in mutate.summary(cold))
    assert _without_via(warm) == cold


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_a_cached_rerun_has_the_same_verdicts(tree, monkeypatch, name):
    """Kills by a test, by a module, survivors and the accepted read back as
    they were measured; the kills by time, which are not kept, are measured
    again."""
    cold = Caching(tree, monkeypatch, by_text, source=TARGETS[name])()
    run = Caching(tree, monkeypatch, by_text, source=TARGETS[name])
    warm = run()
    assert _without_via(warm) == cold
    timeouts = [k for k in warm["kills"] if k["code"] == mutate.TIMED_OUT]
    assert len(run.ran()) == len(timeouts)
    assert not any("via" in k for k in timeouts)


def test_cache_off_writes_nothing_and_asks_for_no_inventory(tree, monkeypatch):
    run = Caching(tree, monkeypatch, world({RAISE: [MINOR]}))
    run(cache=False)
    assert not os.path.exists(os.path.join(tree, ".invective"))
    assert not any(run.asked)
    assert not any(line.startswith("cache:") for line in run.lines)


def test_with_the_cache_off_no_landing_calls_the_store(tree, monkeypatch):
    """The `cache` keyword is a bool, and the cache an object only when it
    is on: nothing is hashed, keyed or kept with it off."""
    def never(*args, **kwargs):
        raise AssertionError("the cache was used with it off")

    for name in ("hash_tree", "campaign_key", "Cache"):
        monkeypatch.setattr(store, name, never)
    report = Caching(tree, monkeypatch, world({RAISE: [MINOR]}))(cache=False)
    assert report["killed"] == 1


# --------------------------------------------------------------------------
# What a run reads


def test_a_verdict_measured_beside_other_runs_is_measured_again_by_one_worker(
        tree, monkeypatch):
    """(SD) A kill or a survivor two copies' runs side by side decided can be
    the other run's doing: a run of one worker measures it again, and what
    it measures is read by the next run of two."""
    said = world({RAISE: [MINOR]})
    Caching(tree, monkeypatch, said)(2)
    assert len(kept(tree)) == 6
    assert all(e["concurrent"] for e in kept(tree).values())
    one = Caching(tree, monkeypatch, said)
    one(1)
    assert sorted(one.ran()) == sorted(ORDER)
    assert not any(e["concurrent"] for e in kept(tree).values())
    two = Caching(tree, monkeypatch, said)
    two(2)
    assert two.ran() == []


def test_with_confirm_no_kill_is_read_back_from_the_cache(tree, monkeypatch):
    """(SD) Asked to confirm at two workers, every kill is run again, even
    one kept by a run of one worker: a test that fails only after another
    is named each time. The survivors are read."""
    def said(run):
        if run.text is not None and BY_TEXT[run.text] == RAISE and not run.alone:
            return killed_by(ADULT)
        return GREEN

    Caching(tree, monkeypatch, said)(1)
    assert kept(tree)[RAISE]["concurrent"] is False
    for _again in range(2):
        run = Caching(tree, monkeypatch, said)
        report = run(2, confirm=True)
        assert set(run.ran()) == {RAISE}
        assert [(u["change"], u["killer"]) for u in report["unreproduced"]] == [
            (RAISE, ADULT)]
        assert {s["via"] for s in report["survivors"]} == {"cache"}


def test_a_confirmed_kill_is_stored_as_measured_alone(tree, monkeypatch):
    """(SD) Confirmed with nothing else running, a kill is kept as one
    measured alone, and a run of one worker reads it; the survivors of the
    pool are not."""
    Caching(tree, monkeypatch, world({RAISE: [MINOR]}))(2, confirm=True)
    entries = kept(tree)
    assert (entries[RAISE]["concurrent"], entries[RAISE]["safe"]) == (
        False, False)
    assert all(entries[what]["concurrent"] for what in ORDER if what != RAISE)
    one = Caching(tree, monkeypatch, world({RAISE: [MINOR]}))
    one(1)
    assert one.ran() == [what for what in ORDER if what != RAISE]


@pytest.mark.parametrize("case", ["concurrent at one worker",
                                  "a kill with confirm", "under the switch",
                                  "a probe's with the history off"])
def test_the_pre_pass_and_the_attempt_agree_on_a_miss(tree, monkeypatch,
                                                      case):
    """(SD) An entry this run may not read is not read before the gate
    either: its mutant's killer is gated and tried, and the mutant run."""
    remember(tree, {RAISE: MINOR})
    said = world({RAISE: [MINOR]})
    first = {"concurrent at one worker": dict(workers=2, history=True),
             "a kill with confirm": dict(workers=1, history=True),
             "under the switch": dict(workers=1),
             "a probe's with the history off": dict(workers=1, history=True)}
    then = {"concurrent at one worker": dict(workers=1, history=True),
            "a kill with confirm": dict(workers=2, history=True, confirm=True),
            "under the switch": dict(workers=1, unsafe_speedups=False),
            "a probe's with the history off": dict(workers=1)}
    Caching(tree, monkeypatch, said)(**first[case])
    assert RAISE in kept(tree)
    run = Caching(tree, monkeypatch, said)
    report = run(**then[case])
    assert RAISE in run.ran()
    assert not any("read from the cache" in line for line in run.lines)
    if then[case].get("history"):
        assert run.gated() == [MINOR]
        assert "history:   1 remembered, 1 usable" in run.lines
        (kill,) = report["kills"]
        assert kill["via"] == "probe"


# --------------------------------------------------------------------------
# --no-unsafe-speedups


@pytest.mark.parametrize("how", ["one worker with the history",
                                 "four workers"])
def test_with_the_switch_on_only_entries_decided_with_it_on_are_read(
        tree, monkeypatch, how):
    """(SD) A run with the unsafe speedups off reads only what a run with
    them off kept, even what one worker without them would have decided the
    same; what it keeps, a run without the switch reads too."""
    said = world({RAISE: [MINOR]})
    first = {"one worker with the history": dict(workers=1, history=True),
             "four workers": dict(workers=4)}[how]
    Caching(tree, monkeypatch, said)(**first)
    assert not any(e["safe"] for e in kept(tree).values())
    switched = Caching(tree, monkeypatch, said)
    switched(unsafe_speedups=False)
    assert sorted(switched.ran()) == sorted(ORDER)
    assert all(e["safe"] for e in kept(tree).values())
    for kwargs in ({"unsafe_speedups": False}, {}):
        again = Caching(tree, monkeypatch, said)
        again(**kwargs)
        assert again.ran() == []


@pytest.mark.parametrize("kwargs, concurrent, safe", [
    ({"unsafe_speedups": False}, False, True),
    ({"workers": 2}, True, False), ({}, False, False)])
def test_a_run_writes_its_mode_in_every_entry(tree, monkeypatch, kwargs,
                                              concurrent, safe):
    Caching(tree, monkeypatch, world({RAISE: [MINOR]}))(**kwargs)
    assert {(e["concurrent"], e["safe"]) for e in kept(tree).values()} == {
        (concurrent, safe)}
    assert len(kept(tree)) == 6


def test_cache_true_with_the_switch_is_neither_refused_nor_overridden(
        tree, monkeypatch):
    """The switch turns the workers and the history off, and leaves the
    cache on, restricted."""
    run = Caching(tree, monkeypatch, world({RAISE: [MINOR]}))
    report = run(3, unsafe_speedups=False, history=True)
    assert report["workers"] == 1 and len(run.places) == 1
    assert "speedups:  safe ones only" in run.lines
    assert len(kept(tree)) == 6


def test_the_cache_line_says_the_restriction(tree, monkeypatch):
    said = world({RAISE: [MINOR]})
    switched = Caching(tree, monkeypatch, said)
    switched(unsafe_speedups=False)
    assert ("cache:     0 on record; only those decided with the unsafe "
            "speedups off are read") in switched.lines
    plain = Caching(tree, monkeypatch, said)
    plain()
    assert "cache:     6 on record" in plain.lines
    again = Caching(tree, monkeypatch, said)
    again(unsafe_speedups=False)
    assert ("cache:     6 on record; only those decided with the unsafe "
            "speedups off are read") in again.lines


# --------------------------------------------------------------------------
# Kills an attempt decided


def test_probe_entries_miss_when_history_is_off_or_tests_hold_options(
        tree, monkeypatch):
    """(SD) A kill the remembered killer made alone is read only while that
    killer would be tried: not with the history off, and not when `--tests`
    holds an option the killer's run would leave out."""
    said = world({RAISE: [MINOR]})
    for case in ("history off", "options"):
        forget(tree)
        remember(tree, {RAISE: MINOR})
        Caching(tree, monkeypatch, said)(history=True)
        assert kept(tree)[RAISE]["origin"] == "probe"
        run = Caching(tree, monkeypatch, said)
        if case == "options":
            with monkeypatch.context() as patched:
                patched.setattr(mutate, "_plain_tests", lambda *args: False)
                run(history=True)
            assert "history:   not used: --tests has options" in run.lines
        else:
            run()
        assert run.ran() == [RAISE]


def _narrowed_kill(run):
    return (killed_by(MINOR) if run.text and "pass" in run.text else GREEN)


def test_coverage_entries_miss_when_coverage_is_off_or_not_used(
        tree, monkeypatch):
    """(SD) A kill by the covering tests alone is read only while they are
    run first: not with coverage off, and not when it cannot be used."""
    for case in ("off", "not used"):
        forget(tree)
        Covering(tree, monkeypatch, REFUSAL, _narrowed_kill)(
            1, coverage=True, cache=True)
        assert kept(tree)[RAISE]["origin"] == "coverage"
        run = Covering(tree, monkeypatch, REFUSAL, _narrowed_kill)
        if case == "not used":
            with monkeypatch.context() as patched:
                patched.setattr(mutate, "_coverage_unavailable",
                                lambda: "coverage is not installed")
                report = run(1, coverage=True, cache=True)
            assert report["coverage"]["unused"] == "coverage is not installed"
        else:
            run(1, cache=True)
        assert run.ran() == [(RAISE, "selection")]


def test_a_cached_probe_kill_keeps_its_origin_and_is_counted_on_the_probe_line(
        tree, monkeypatch):
    remember(tree, {RAISE: MINOR})
    said = world({RAISE: [MINOR]})
    Caching(tree, monkeypatch, said)(history=True)
    run = Caching(tree, monkeypatch, said)
    report = run(history=True)
    (kill,) = report["kills"]
    assert (kill["via"], kill["origin"], kill["killer"]) == ("cache", "probe",
                                                             MINOR)
    assert run.gated() == [] and run.ran() == []
    assert mutate.summary(report)[1:] == [
        "           1 of the kills were a remembered killer run alone",
        "           6 of the verdicts were read from .invective/cache, not run"]


def test_a_cached_coverage_kill_keeps_its_origin_and_is_counted_on_the_coverage_line(
        tree, monkeypatch):
    Covering(tree, monkeypatch, REFUSAL, _narrowed_kill)(1, coverage=True,
                                                         cache=True)
    run = Covering(tree, monkeypatch, REFUSAL, _narrowed_kill)
    report = run(1, coverage=True, cache=True)
    (kill,) = report["kills"]
    assert (kill["via"], kill["origin"]) == ("cache", "coverage")
    assert run.ran() == []
    assert mutate.summary(report)[1:] == [
        "           1 of the kills were the covering tests run alone",
        "           6 of the verdicts were read from .invective/cache, not run"]


def _two_attempts(run):
    """The refusal's mutant killed by its covering test alone, and the
    `and`'s by its remembered killer alone."""
    if run.text is None:
        return GREEN
    if BY_TEXT[run.text] == RAISE:
        return killed_by(MINOR)
    if BY_TEXT[run.text] == BOOL and run.kind in ("probe", "selection"):
        return killed_by(ADULT)
    return GREEN


def test_a_hit_is_not_put_again_and_keeps_its_origin(tree, monkeypatch):
    """(SD) Put again, a kill read back would lose its origin, and be read
    with the attempt that made it off: read twice, each entry is as it was
    written, and with both attempts off neither is read."""
    remember(tree, {BOOL: ADULT})
    on = dict(history=True, coverage=True, cache=True)
    first = Covering(tree, monkeypatch, REFUSAL, _two_attempts)
    first(1, **on)
    entries = kept(tree)
    assert (entries[RAISE]["origin"], entries[BOOL]["origin"]) == (
        "coverage", "probe")
    paths = sorted(glob.glob(os.path.join(tree, ".invective", "cache", "*",
                                          "*", "*.json")))
    written = {path: (os.stat(path).st_mtime_ns, open(path, "rb").read())
               for path in paths}
    for _again in range(2):
        run = Covering(tree, monkeypatch, REFUSAL, _two_attempts)
        report = run(1, **on)
        assert run.ran() == []
        assert {(k["change"], k["origin"]) for k in report["kills"]} == {
            (RAISE, "coverage"), (BOOL, "probe")}
        assert {path: (os.stat(path).st_mtime_ns, open(path, "rb").read())
                for path in paths} == written
    off = Covering(tree, monkeypatch, REFUSAL, _two_attempts)
    off(1, cache=True)
    assert sorted(off.ran()) == sorted([(RAISE, "selection"),
                                        (BOOL, "selection")])


def test_a_full_selection_entry_hits_whatever_the_feature_flags(
        tree, monkeypatch):
    """(SD) A verdict of the whole selection is what every run comes to in
    the end, the probe and the covering tests on or off: a survivor
    included, which they never make."""
    Covering(tree, monkeypatch, REFUSAL, _two_attempts)(1, cache=True)
    assert {e["origin"] for e in kept(tree).values()} == {""}
    remember(tree, {RAISE: MINOR, BOOL: ADULT})
    run = Covering(tree, monkeypatch, REFUSAL, _two_attempts)
    report = run(1, history=True, coverage=True, cache=True)
    assert run.ran() == [] and run.of("probe") == []
    assert len(report["survivors"]) == 4
    assert "history:   2 remembered, 0 usable, 2 read from the cache" in (
        run.lines)


# --------------------------------------------------------------------------
# What is never kept


def test_a_timeout_is_never_cached(tree, monkeypatch):
    """(SD) A kill by time can be the machine's load."""
    def said(run):
        if run.text is not None and BY_TEXT[run.text] == RAISE:
            return TIMEOUT
        return GREEN

    Caching(tree, monkeypatch, said)()
    assert RAISE not in kept(tree) and len(kept(tree)) == 5
    run = Caching(tree, monkeypatch, said)
    run()
    assert run.ran() == [RAISE]


def test_a_narrowed_timeout_is_never_cached(tree, monkeypatch):
    """(SD) Nor one the covering tests alone ran out of time on, marked as
    theirs."""
    def said(run):
        return TIMEOUT if run.text and "pass" in run.text else GREEN

    report = Covering(tree, monkeypatch, REFUSAL, said)(1, coverage=True,
                                                        cache=True)
    (kill,) = report["kills"]
    assert (kill["via"], kill["code"]) == ("coverage", mutate.TIMED_OUT)
    assert RAISE not in kept(tree)
    run = Covering(tree, monkeypatch, REFUSAL, said)
    run(1, coverage=True, cache=True)
    assert run.ran() == [(RAISE, "narrowed")]


@pytest.mark.parametrize("verdict", [
    mutate.Verdict(False, -9, "", ""),
    killed_by(MINOR, ExitCode.INTERNAL_ERROR),
    killed_by("pkg/tests/test_gate.py", ExitCode.USAGE_ERROR),
    killed_by("pkg/tests/test_gate.py", ExitCode.NO_TESTS_COLLECTED),
    mutate.Verdict(False, ExitCode.TESTS_FAILED, "1 failed", "")],
    ids=["signal", "internal", "usage", "no-tests", "no-killer"])
def test_a_harness_or_signal_kill_is_never_cached(tree, monkeypatch, verdict):
    """(SD) A run a signal or a crash ended, the harness's own codes, and a
    kill that named nothing: none is a test failing on the mutant."""
    def said(run):
        if run.text is not None and BY_TEXT[run.text] == RAISE:
            return verdict
        return GREEN

    Caching(tree, monkeypatch, said)()
    assert RAISE not in kept(tree) and len(kept(tree)) == 5
    run = Caching(tree, monkeypatch, said)
    run()
    assert run.ran() == [RAISE]


# --------------------------------------------------------------------------
# Resume


def _stopping_at(campaign, at, said):
    """*said*, but a ^C on the run of the mutant whose change is *at*."""
    def stopping(run):
        if run.text is not None and BY_TEXT[run.text] == at:
            _thread.interrupt_main()
            assert campaign[0].stop.wait(30)
            raise mutate.Stopped
        return said(run)
    return stopping


def test_an_interrupted_run_resumes_where_it_stopped(tree, monkeypatch):
    said = world({RAISE: [MINOR], "Lt -> LtE": [ADULT]})
    campaign = []
    campaign.append(Caching(tree, monkeypatch,
                            _stopping_at(campaign, ORDER[3], said)))
    with pytest.raises(KeyboardInterrupt):
        campaign[0]()
    assert sorted(kept(tree)) == sorted(ORDER[:3])
    run = Caching(tree, monkeypatch, said)
    resumed = run()
    assert run.ran() == ORDER[3:]
    cold = Caching(tree, monkeypatch, said)
    forget(tree)
    assert _without_via(resumed) == _without_via(cold())


def test_each_verdict_is_on_disk_as_it_lands(tree, monkeypatch):
    """Not at the end: a run killed outright keeps what landed."""
    said = world({RAISE: [MINOR]})
    seen = []

    def counting(run):
        if run.text is not None:
            seen.append(len(kept(tree)))
        return said(run)

    Caching(tree, monkeypatch, counting)()
    assert seen == list(range(6))


def _stop_in_the_pool(campaign, said):
    """At two workers, a ^C as the first two mutants run: the first copy's
    survives, the second's is killed."""
    def stopping(run):
        if run.text is None or run.alone:
            return said(run)
        if len([r for r in campaign[0].runs if r.text]) == 2:
            _thread.interrupt_main()
        assert campaign[0].stop.wait(30)
        return (GREEN, killed_by(MINOR))[run.copy]
    return stopping


def test_a_stop_without_confirm_keeps_its_kills_for_the_resume(
        tree, monkeypatch):
    campaign = []
    campaign.append(Caching(tree, monkeypatch, _stop_in_the_pool(
        campaign, world({}))))
    with pytest.raises(KeyboardInterrupt):
        campaign[0](2)
    entries = kept(tree)
    assert sorted(entries) == sorted(ORDER[:2])
    assert {e["concurrent"] for e in entries.values()} == {True}
    run = Caching(tree, monkeypatch, world({}))
    run(2)
    assert sorted(run.ran()) == sorted(ORDER[2:])


def test_a_stop_before_confirmation_keeps_survivors_only(tree, monkeypatch):
    """A kill made beside another run lands once confirmed, and the stop
    came first."""
    campaign = []
    campaign.append(Caching(tree, monkeypatch, _stop_in_the_pool(
        campaign, world({}))))
    with pytest.raises(KeyboardInterrupt):
        campaign[0](2, confirm=True)
    entries = kept(tree)
    assert list(entries) == [ORDER[0]] and entries[ORDER[0]]["ok"]


def test_a_stop_during_confirmation_keeps_the_kills_confirmed_so_far(
        tree, monkeypatch):
    said = world({RAISE: [MINOR], BOOL: [ADULT]})
    first, second = sorted((RAISE, BOOL), key=ORDER.index)
    campaign = []

    def stopping(run):
        if run.text is not None and run.alone and BY_TEXT[run.text] == second:
            _thread.interrupt_main()
            assert campaign[0].stop.wait(30)
            raise mutate.Stopped
        return said(run)

    campaign.append(Caching(tree, monkeypatch, stopping))
    with pytest.raises(KeyboardInterrupt):
        campaign[0](2, confirm=True)
    entries = kept(tree)
    assert second not in entries
    assert (entries[first]["ok"], entries[first]["concurrent"]) == (False,
                                                                   False)
    assert sorted(entries) == sorted(w for w in ORDER if w != second)
    assert all(entries[w]["concurrent"] for w in ORDER if w not in (first,
                                                                    second))


def test_a_refused_run_keeps_what_landed(tree, monkeypatch):
    said = world({RAISE: [MINOR]})

    def refusing(run):
        if run.text is not None and BY_TEXT[run.text] == ORDER[2]:
            return mutate.Verdict(False, ExitCode.USAGE_ERROR, "", "")
        return said(run)

    with pytest.raises(mutate.Refusal):
        Caching(tree, monkeypatch, refusing)()
    assert sorted(kept(tree)) == sorted(ORDER[:2])


def test_a_refusal_before_the_key_creates_no_cache(tree, monkeypatch):
    with pytest.raises(mutate.Refusal):
        Caching(tree, monkeypatch, lambda run: killed_by(MINOR))()
    assert not os.path.exists(os.path.join(tree, ".invective"))


# --------------------------------------------------------------------------
# The gate and the order of the header


def test_the_gate_skips_killers_of_cached_sites(tree, monkeypatch):
    """A site whose verdict is read is never probed, so its killer is not
    run alone on the original."""
    remember(tree, {RAISE: MINOR, BOOL: ADULT})
    said = world({RAISE: [MINOR], BOOL: [ADULT]})
    cold = Caching(tree, monkeypatch, said)
    cold(history=True)
    assert cold.gated() == [MINOR, ADULT]
    os.remove(glob.glob(os.path.join(tree, ".invective", "cache", "*", "*",
                                     store._hex(SITES[BOOL].text)[:32]
                                     + ".json"))[0])
    run = Caching(tree, monkeypatch, said)
    run(history=True)
    assert run.gated() == [ADULT] and run.probed() == [(BOOL, ADULT)]
    assert "history:   2 remembered, 1 usable, 1 read from the cache" in (
        run.lines)


def test_the_history_line_stays_when_every_remembered_site_hits(
        tree, monkeypatch):
    """Without the clause, a re-run the cache answers whole would read as
    every killer failing its gate."""
    remember(tree, {RAISE: MINOR, BOOL: ADULT})
    said = world({RAISE: [MINOR], BOOL: [ADULT]})
    cold = Caching(tree, monkeypatch, said)
    cold(history=True)
    assert "history:   2 remembered, 2 usable" in cold.lines
    run = Caching(tree, monkeypatch, said)
    run(history=True)
    assert "history:   2 remembered, 0 usable, 2 read from the cache" in (
        run.lines)
    assert run.gated() == []


def test_coverage_runs_before_the_pre_pass_and_its_lines_keep_their_order(
        tree, monkeypatch):
    """Whether coverage is in use is which kills may be read, so its run is
    first; the header says the history, the coverage, then the cache."""
    remember(tree, {BOOL: ADULT})
    run = Covering(tree, monkeypatch, REFUSAL, _two_attempts)
    run(1, history=True, coverage=True, cache=True)
    kinds = [(r.kind, r.text is None) for r in run.runs]
    assert kinds[:3] == [("selection", True), ("coverage", True),
                         ("probe", True)]
    heads = [line.split(":")[0] for line in run.lines
             if line.startswith(("history:", "coverage:", "cache:",
                                 "mutants:"))]
    assert heads == ["history", "coverage", "cache", "mutants"]


def test_a_cold_run_with_the_cache_on_starts_the_runs_of_one_with_it_off(
        tree, monkeypatch):
    remember(tree, {RAISE: MINOR})
    said = world({RAISE: [MINOR]})
    off = Caching(tree, monkeypatch, said)
    off(2, history=True, cache=False)
    remember(tree, {RAISE: MINOR})
    on = Caching(tree, monkeypatch, said)
    on(2, history=True)
    def made(run):
        # Which copy runs which mutant is whichever is free first.
        return collections.Counter(r._replace(copy=None) for r in run.runs)

    assert made(on) == made(off)
    assert len(on.runs) == 3 + 1 + 6


# --------------------------------------------------------------------------
# When the cache cannot be used


def test_a_key_that_cannot_be_built_says_so_and_the_cache_is_not_used(
        tree, monkeypatch):
    said = world({RAISE: [MINOR]})
    run = Caching(tree, monkeypatch, said, imported=None)
    run()
    assert ("cache:     not used: the baseline did not say which files it "
            "imported") in run.lines
    assert not os.path.exists(os.path.join(tree, ".invective"))

    def unreadable(*args):
        raise PermissionError("no access")

    for name, said_ in (("hash_tree", "the copy could not be read: no access"),
                        ("campaign_key", "the campaign's files could not be "
                         "read: no access")):
        with monkeypatch.context() as patched:
            patched.setattr(store, name, unreadable)
            run = Caching(tree, monkeypatch, said)
            report = run()
        assert "cache:     not used: %s" % said_ in run.lines
        assert len(run.ran()) == 6 and report["killed"] == 1
        assert not os.path.exists(os.path.join(tree, ".invective"))


def test_an_unwritable_store_is_said_once_and_the_run_goes_on(
        tree, monkeypatch):
    with open(os.path.join(tree, ".invective"), "w", encoding="utf-8") as fh:
        fh.write("a file")
    run = Caching(tree, monkeypatch, world({RAISE: [MINOR]}))
    report = run()
    warned = [line for line in run.lines if line.startswith("warning:")]
    assert len(warned) == 1 and "no verdict is kept" in warned[0]
    assert report["killed"] == 1 and len(run.ran()) == 6


def test_an_entry_that_cannot_be_read_is_said_once_and_measured(
        tree, monkeypatch):
    said = world({RAISE: [MINOR]})
    Caching(tree, monkeypatch, said)()
    (path,) = glob.glob(os.path.join(tree, ".invective", "cache", "*", "*",
                                     store._hex(SITES[RAISE].text)[:32]
                                     + ".json"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{")
    remember(tree, {RAISE: MINOR})
    run = Caching(tree, monkeypatch, said)
    run(history=True)
    assert run.ran() == [RAISE] and run.gated() == [MINOR]
    warned = [line for line in run.lines if line.startswith("warning:")]
    assert warned == ["warning:   1 verdict(s) kept in %s could not be read, "
                      "and were measured again" % os.path.dirname(path)]


def test_a_stop_while_hashing_leaves_nothing_behind(tree, monkeypatch):
    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "hash_tree", interrupted)
    run = Caching(tree, monkeypatch, world({RAISE: [MINOR]}))
    with pytest.raises(KeyboardInterrupt):
        run(2)
    assert len(run.places) == 2
    assert not any(os.path.exists(where) for where in run.places)
    assert run.runs == []
    assert not os.path.exists(os.path.join(tree, ".invective"))


# --------------------------------------------------------------------------
# Real runs


def _settings(tree, extra=""):
    write_tree(tree, {"pyproject.toml": "[tool.pytest.ini_options]\n"
                      "[tool.invective]\nhistory = false\ncache = true\n"
                      + extra})


def _run(tree, *args):
    return subprocess.run(
        [sys.executable, "-m", "invective", "run", "--target", GATE,
         "--tests", *GATE_TESTS, "--json", "r.json", *args],
        cwd=tree, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": SRC})


def _report(tree):
    with open(os.path.join(tree, "r.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_a_second_real_run_is_read_from_the_cache_with_the_same_verdicts(
        tree):
    _settings(tree)
    first = _run(tree)
    assert first.returncode == 0, first.stderr
    cold = _report(tree)
    assert "cache:     0 on record" in first.stdout
    second = _run(tree)
    assert second.returncode == 0, second.stderr
    warm = _report(tree)
    assert _without_via(warm) == cold
    assert "cache:     6 on record" in second.stdout
    assert ("6 of the verdicts were read from .invective/cache, not run"
            in second.stdout)
    # An edit of the tests is a fresh campaign.
    with open(os.path.join(tree, "pkg", "tests", "test_gate.py"), "a",
              encoding="utf-8") as fh:
        fh.write("\n# an edit\n")
    third = _run(tree)
    assert "cache:     0 on record" in third.stdout
    assert not any("via" in k for k in _report(tree)["kills"])


def test_a_second_real_run_on_a_ref_is_read_from_the_cache(repo):
    """Each run's worktree is in a temporary directory of its own, and the
    key is the same."""
    _settings(repo)
    commit(repo)
    first = _run(repo, "--ref", "main")
    assert first.returncode == 0, first.stderr
    second = _run(repo, "--ref", "main")
    assert second.returncode == 0, second.stderr
    assert "cache:     6 on record" in second.stdout
    assert {e["via"] for key in ("kills", "survivors")
            for e in _report(repo)[key]} == {"cache"}


def test_a_run_with_the_cache_on_leaves_the_project_as_it_was_but_for_invective(
        tree):
    """Every mutant is written in the copy, and the one thing a run writes
    in the project is `.invective/`."""
    _settings(tree)

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
    assert os.path.join(".invective", ".gitignore") in added
    assert len(added) == 1 + 6
    assert all(name.startswith(".invective" + os.sep) for name in added)
    assert {name: after[name] for name in before} == before
