"""Workers: mutants run at once, each in a copy of its own, and every kill
made beside other runs confirmed with nothing else running.

Most of these drive the engine with a stand-in for `run_tests` that decides
each run from the copy it is in and the text of the target there, so which
copy runs what, and in what order, is under the test's control.
"""

from __future__ import annotations

import _thread
import collections
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from pytest import ExitCode

from invective import config, mutate
from invective import tree as trees

from conftest import FILES, SRC, commit, git, stop_group, wait_for, write_tree
from test_mutate import ACCEPTING, NESTED

GATE = os.path.join("pkg", "gate.py")
GATE_TESTS = ["pkg/tests/test_gate.py"]
MINOR = "pkg/tests/test_gate.py::test_a_minor_is_refused"
ADULT = "pkg/tests/test_gate.py::test_an_adult_is_admitted"


class Run(NamedTuple):
    """One run, as the stand-in saw it."""

    #: The copy, by the order its `copy:` line was said: 0 is the first.
    copy: int
    #: The target's text in the copy, None when it is the original.
    text: str | None
    #: The one test a killer's file names, `""` for the selection.
    alone: str
    timeout: float


class Campaign:
    """The fixture tree with *source* as `pkg/gate.py`, and the engine run
    on it with `run_tests` standing in: each run is what `said(run)` says,
    green on the original unless it says otherwise."""

    def __init__(self, tree, monkeypatch, source=FILES["pkg/gate.py"],
                 said=None):
        self.tree, self.source = tree, source
        self.said = said or (lambda run: GREEN)
        self.places, self.lines, self.runs = [], [], []
        self.lock = threading.Lock()
        write_tree(tree, {"pkg/gate.py": source})
        monkeypatch.setattr(mutate, "run_tests", self.run_tests)

    def say(self, line):
        self.lines.append(line)
        if line.startswith("copy:"):
            self.places.append(line.split(None, 1)[1])

    def run_tests(self, where, tests, timeout, selection=None, options=(),
                  target="", stop=None):
        # One event for the whole campaign, which a `said` can wait on.
        self.stop = stop
        with open(os.path.join(where, GATE), encoding="utf-8",
                  newline="") as fh:
            text = fh.read()
        alone = ""
        if selection is not None and os.path.basename(selection).startswith(
                "killer-"):
            with open(selection, encoding="utf-8") as fh:
                (alone,) = fh.read().splitlines()
        run = Run(self.places.index(where),
                  None if text == self.source else text, alone, timeout)
        with self.lock:
            self.runs.append(run)
        return self.said(run)

    def __call__(self, workers, only=None, limit=None, **kwargs):
        return mutate.mutate(self.tree, os.path.join(self.tree, GATE),
                             kwargs.pop("tests", GATE_TESTS), only, limit,
                             say=self.say, workers=workers, **kwargs)


GREEN = mutate.Verdict(True, 0, "2 passed", "")


def killed_by(killer, code=ExitCode.TESTS_FAILED):
    return mutate.Verdict(False, code, "1 failed", killer)


@pytest.fixture
def landed(monkeypatch):
    """Every outcome the engine says stands, as (place, outcome)."""
    seen = []
    monkeypatch.setattr(mutate, "_landed", lambda mutant, outcome: seen.append(
        (mutant.job.n, outcome)))
    return seen


# --------------------------------------------------------------------------
# One worker is the serial campaign


#: The targets the reports are pinned for: every kind, nested; acceptances,
#: some stale; a CRLF file; and more than ten mutants.
TARGETS = {
    "gate": FILES["pkg/gate.py"],
    "nested": NESTED,
    "accepting": ACCEPTING,
    "crlf": FILES["pkg/gate.py"].replace("\n", "\r\n"),
    "many": "X = [%s]\n" % ", ".join(str(n) for n in range(12)),
}

#: The report each target gives with one worker and `by_text` deciding its
#: runs, `target` aside (it is spelt with this platform's separators), as
#: the SHA-256 of its JSON with sorted keys. What they pin is every entry,
#: its diff, killer and code, the lists they are in and their order, the
#: counts, and the stale acceptances, so that a campaign with one worker
#: reports what a serial one does, entry for entry, but for `workers`.
PINNED = {
    "gate": "52926e48e8af12945decfbc6c6f43d5ea8291b82c96603f0de0cd441af23e0f3",
    "nested": "21c91663678292772bf6ee8f626f7a62928ca22a41bf070997a815be3644bb2a",
    "accepting": "137f6cc8d060824ee6f30b3b0572dbadb9b31c01a315c11c5865832643defc0a",
    "crlf": "f5d249197a8c24f17da514d1ca02622cb936d4e60f18b49bdd0a63e070af0d58",
    "many": "426bce67f2e4bbcc712328bf2718bf9f8b3b4fd026b751fc5a520cbbfe5b1bcc",
}


def by_text(run):
    """A verdict for every text, the same wherever and whenever it is run:
    a survivor, a kill by either of two tests, a module that would not
    import, or a timeout. A killer run alone fails as it did in the
    selection, and passes on the original."""
    if run.text is None:
        return GREEN
    pick = hashlib.sha256(run.text.encode("utf-8")).digest()[0] % 6
    if pick < 2:
        return GREEN
    if pick < 4:
        return killed_by((MINOR, ADULT)[pick - 2])
    if pick == 4:
        return killed_by("pkg/tests/test_gate.py", ExitCode.INTERRUPTED)
    return mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT")


def _digest(report):
    kept = {key: value for key, value in report.items()
            if key not in ("target", "workers")}
    return hashlib.sha256(json.dumps(kept, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_one_worker_reports_as_a_serial_campaign_does(tree, monkeypatch,
                                                     name):
    """Every report field but `workers` is what a serial campaign gives, on
    the command line's path and on the plugin's."""
    run = Campaign(tree, monkeypatch, TARGETS[name], by_text)
    report = run(1)
    assert report["workers"] == 1
    assert report["target"] == GATE
    assert _digest(report) == PINNED[name], json.dumps(report, indent=1)

    plugin = run(1, selection=[MINOR, ADULT])
    assert plugin["tests"] == [MINOR, ADULT]
    assert {**plugin, "tests": GATE_TESTS} == report


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_three_workers_report_as_one_does_with_every_kill_confirmed(
        tree, monkeypatch, name):
    """The same entries in the same order, from runs that ended in another
    order: each kill carries how it was confirmed, and nothing else
    differs. A killer that is a test failed alone, and the rest were the
    whole selection again."""
    one = Campaign(tree, monkeypatch, TARGETS[name], by_text)(1)
    three = Campaign(tree, monkeypatch, TARGETS[name], by_text)(3)

    assert (one["workers"], three["workers"]) == (1, 3)
    assert not any("confirmed" in entry for key in ("kills", "survivors",
                                                    "accepted")
                   for entry in one[key])
    assert [kill["confirmed"] for kill in three["kills"]] == [
        "alone" if "::" in kill["killer"] else "full"
        for kill in three["kills"]]
    assert not any("confirmed" in entry for key in ("survivors", "accepted")
                   for entry in three[key])
    stripped = {**three, "workers": 1, "kills": [
        {key: value for key, value in kill.items() if key != "confirmed"}
        for kill in three["kills"]]}
    assert stripped == one


def test_the_progress_line_counts_the_mutants_that_have_ended(
        tree, monkeypatch):
    """One worker, a line after every tenth: the copy's, then the count."""
    run = Campaign(tree, monkeypatch, TARGETS["many"])
    run(1, only=["CONST"])
    assert run.lines[:2] == ["copy:      %s" % run.places[0], "workers:   1"]
    assert [ln for ln in run.lines if ln.startswith("  ...")] == [
        "  ... 10/12 landed"]


# --------------------------------------------------------------------------
# The copies


def test_each_worker_has_a_copy_of_its_own_said_before_the_rest(
        tree, monkeypatch):
    run = Campaign(tree, monkeypatch)
    report = run(3)
    assert report["workers"] == 3
    assert len(set(run.places)) == 3
    assert run.lines[:5] == ["copy:      %s" % where for where in run.places] + [
        "workers:   3", "project:   %s" % tree]
    assert {r.copy for r in run.runs} == {0, 1, 2}
    assert not any(os.path.exists(where) for where in run.places)


def test_auto_is_at_most_one_worker_a_mutant(tree, monkeypatch):
    monkeypatch.setattr(config, "_cpus", lambda: 64)
    run = Campaign(tree, monkeypatch, TARGETS["many"])
    assert run("auto", limit=3)["workers"] == 3
    assert run("auto")["workers"] == config.AUTO_MOST


def test_no_copy_holds_what_a_run_left_in_another(tree, monkeypatch):
    """Every copy is made before the first run: one made after would hold
    what the baseline wrote in the first, a file a test reads."""
    def said(run):
        place = run.copy
        left = os.path.join(campaign.places[place], "left.txt")
        if place == 0:
            with open(left, "w", encoding="utf-8") as fh:
                fh.write("from the first copy's baseline")
        elif os.path.exists(left):
            return killed_by(MINOR)
        return GREEN

    campaign = Campaign(tree, monkeypatch, said=said)
    report = campaign(2, only=["RAISE"])
    assert report["survivors"] and not report["kills"]


def test_a_copy_red_or_short_on_the_unmutated_tree_is_refused(
        tree, monkeypatch):
    """Each copy runs the selection at once with the others, and each must
    be green on its own: a test red only beside another copy's run, or a
    copy missing a test, would score kills that are no mutant's."""
    for wrong, says in [(killed_by(MINOR), "is RED on the unmutated tree at"),
                        (GREEN._replace(missing=(MINOR,)),
                         "1 of the tests selected are not in the tree"),
                        (GREEN._replace(elsewhere="/elsewhere/gate.py"),
                         "was imported from /elsewhere/gate.py")]:
        written = []
        monkeypatch.setattr(mutate, "_write", lambda *a: written.append(a))
        run = Campaign(tree, monkeypatch, said=lambda r, wrong=wrong: (
            wrong if r.copy == 2 else GREEN))
        with pytest.raises(mutate.Refusal) as caught:
            run(3)
        assert says in str(caught.value)
        assert not written
        assert not any(os.path.exists(where) for where in run.places)


def test_the_first_copy_alone_is_checked_before_the_others_run(
        tree, monkeypatch):
    """Red alone is refused before any copy runs at once with another, and
    before any mutant is written."""
    written = []
    monkeypatch.setattr(mutate, "_write", lambda *a: written.append(a))
    run = Campaign(tree, monkeypatch, said=lambda r: killed_by(MINOR))
    with pytest.raises(mutate.Refusal) as caught:
        run(2)
    assert "is RED on the unmutated tree, so" in str(caught.value)
    assert run.runs == [Run(0, None, "", mutate.BASELINE_TIMEOUT)]
    assert not written


def test_every_copy_of_a_ref_is_the_commit_the_first_copy_holds(
        repo, monkeypatch):
    """The first copy is a worktree of the ref; the others are of the
    commit it holds, and not of the ref again, which can name another
    commit by then."""
    before = git(repo, "rev-parse", "HEAD").strip()
    made = []
    real = mutate.git_ref

    @contextlib.contextmanager
    def git_ref(root, ref):
        made.append(ref)
        with real(root, ref) as where:
            if len(made) == 1:
                commit(repo, {"pkg/gate.py": FILES["pkg/gate.py"]
                              + "# moved\n"})
            yield where

    monkeypatch.setattr(mutate, "git_ref", git_ref)
    heads = set()

    def said(r):
        heads.add(git(run.places[r.copy], "rev-parse", "HEAD").strip())
        return GREEN

    run = Campaign(repo, monkeypatch, said=said)
    report = run(3, ref="main")
    assert report["workers"] == 3
    assert made == ["main", before, before]
    assert heads == {before}
    assert git(repo, "rev-parse", "HEAD").strip() != before


@pytest.mark.parametrize("alone, together", [(20, 0), (0, 15)])
def test_a_kill_is_confirmed_on_no_less_time_than_the_loaded_budget(
        tree, monkeypatch, alone, together):
    """The budget is three times the slowest run of the selection at once;
    a confirmation, with nothing else running, gets three times the first
    copy's run alone, and never less than the budget."""
    now = [1_000_000.0]
    monkeypatch.setattr(mutate, "time", SimpleNamespace(
        time=lambda: now[0], monotonic=time.monotonic))
    baselines = []

    def said(run):
        if run.text is None and run.timeout == mutate.BASELINE_TIMEOUT:
            baselines.append(run.copy)
            now[0] += alone if len(baselines) == 1 else together
            return GREEN
        return GREEN if run.text is None else killed_by(
            "pkg/tests/test_gate.py")

    run = Campaign(tree, monkeypatch, said=said)
    report = run(2, only=["RAISE"])

    assert report["kills"][0]["confirmed"] == "full"
    # Three baselines, the mutant's run in the pool, then the first copy
    # restored and run, and the mutant run there again.
    pooled, guard, again = run.runs[3:]
    assert (guard.text, again.text) == (None, pooled.text)
    if alone:
        assert pooled.timeout == 30.0
        assert guard.timeout == again.timeout == 3 * alone
    else:
        assert pooled.timeout >= 3 * together
        assert guard.timeout == again.timeout == pooled.timeout


# --------------------------------------------------------------------------
# The pool


def test_two_workers_run_two_mutants_at_once(tree, monkeypatch):
    """Each mutant's run waits for the other's to start: run one after the
    other, the first would wait out the barrier's time and the campaign
    would be refused."""
    barrier = threading.Barrier(2, timeout=30)

    def said(run):
        if run.text is not None:
            barrier.wait()
        return GREEN

    campaign = Campaign(tree, monkeypatch, said=said)
    report = campaign(2, only=["RAISE", "BOOL"])
    assert report["mutants"] == len(report["survivors"]) == 2
    assert {r.copy for r in campaign.runs if r.text} == {0, 1}


def test_mutants_are_made_on_the_main_thread_only(tree, monkeypatch):
    """`_text_of` sets the process's warning filters aside and back, which
    two threads at once would leave set aside."""
    threads = []
    real = mutate._text_of

    def text_of(module, index):
        threads.append(threading.current_thread())
        return real(module, index)

    monkeypatch.setattr(mutate, "_text_of", text_of)
    run = Campaign(tree, monkeypatch, said=lambda r: killed_by(MINOR)
                   if r.text else GREEN)
    report = run(3)
    # Each mutant once in the pool, and each kill once more to confirm it.
    assert len(threads) == report["mutants"] + report["killed"]
    assert set(threads) == {threading.main_thread()}


def test_each_copy_s_writes_are_stamped_at_rising_seconds(tree, monkeypatch):
    """Two mutants of one line are often the same size, and a stale `.pyc`
    is keyed on the size and the second: in every copy, every write is a
    later second than the one before, confirmations and the original
    written back included."""
    stamps = {}
    real = mutate._write

    def write(path, text, when):
        stamps.setdefault(os.path.dirname(os.path.dirname(path)), []).append(
            when)
        real(path, text, when)

    monkeypatch.setattr(mutate, "_write", write)
    run = Campaign(tree, monkeypatch, said=lambda r: killed_by(MINOR)
                   if r.text else GREEN)
    report = run(2)
    assert report["mutants"] == 6
    assert len(stamps) == 2
    first = stamps[run.places[0]]
    # Its share of the pool, then the original back and each kill again, a
    # second apart, past every place a mutant was stamped at.
    restored = first[-7:]
    assert len(first) >= 8
    assert restored == list(range(restored[0], restored[0] + 7))
    assert restored[0] > max(stamps[run.places[1]])
    for written in stamps.values():
        assert all(a < b for a, b in zip(written, written[1:])), written


def test_a_kill_its_killer_does_not_make_alone_is_the_whole_selection_s_again(
        tree, monkeypatch, landed):
    """In the second copy every mutant is killed, as a test sharing a port
    with the first copy's run would kill it; alone, the killer passes, so
    the whole selection is run again with nothing else running, and finds
    a survivor."""
    def said(run):
        if run.copy == 0:
            return GREEN
        return killed_by(MINOR) if run.text else GREEN

    run = Campaign(tree, monkeypatch, said=said)
    report = run(2)

    assert report["kills"] == []
    confirmed = [s for s in report["survivors"] if "confirmed" in s]
    assert confirmed and all(s["confirmed"] == "full" for s in confirmed)
    # Alone on the original, then alone on each mutant, then the whole
    # selection on each.
    alone = [r for r in run.runs if r.alone]
    assert alone[0] == Run(0, None, MINOR, alone[0].timeout)
    assert len(alone) == 1 + len(confirmed)
    assert len(landed) == report["mutants"]


@pytest.mark.parametrize("tests, gated", [
    (GATE_TESTS, True), (GATE_TESTS + ["-k", "minor or adult"], False)])
def test_a_killer_is_run_alone_only_where_it_leaves_out_nothing_the_tests_chose(
        tree, monkeypatch, tests, gated):
    """On the command line, `--tests` can hold an option a run of one test
    would drop with them, so then the kill is confirmed by the whole
    selection."""
    run = Campaign(tree, monkeypatch, said=lambda r: killed_by(MINOR)
                   if r.text else GREEN)
    report = run(2, only=["RAISE"], tests=tests)
    (kill,) = report["kills"]
    assert kill["confirmed"] == ("alone" if gated else "full")
    assert bool([r for r in run.runs if r.alone]) is gated


def test_a_killer_red_alone_on_the_original_confirms_nothing_alone(
        tree, monkeypatch):
    """A test that fails alone on the original, because it needs another
    to run first, fails alone on every mutant too: its kills go to the
    whole selection again, which here kills them as well."""
    def said(run):
        if run.alone or run.text:
            return killed_by(MINOR)
        return GREEN

    run = Campaign(tree, monkeypatch, said=said)
    report = run(2)
    assert {kill["confirmed"] for kill in report["kills"]} == {"full"}
    assert [r for r in run.runs if r.alone] == [
        Run(0, None, MINOR, run.runs[-1].timeout)]


def test_a_kill_read_back_is_never_confirmed(tree, monkeypatch, landed):
    """A kill an attempt says was read back was confirmed when it was kept:
    it lands as it ends, and no run confirms it. The others are."""
    real = mutate._measure

    def measure(mutant, copy, attempts, budget):
        got = real(mutant, copy, attempts, budget)
        return got._replace(via="cache") if mutant.job.n == 1 else got

    monkeypatch.setattr(mutate, "_measure", measure)
    run = Campaign(tree, monkeypatch, said=lambda r: killed_by(MINOR)
                   if r.text else GREEN)
    report = run(2)
    first, *others = report["kills"]
    assert first["via"] == "cache" and "confirmed" not in first
    assert {kill["confirmed"] for kill in others} == {"alone"}
    # The kill read back was run once, in the pool; each other kill once
    # more, its killer alone.
    texts = collections.Counter(r.text for r in run.runs if r.text)
    assert sorted(texts.values()) == [1] + [2] * 5
    assert [n for n, _o in landed][0] == 1


def test_with_only_kills_read_back_nothing_is_confirmed(tree, monkeypatch):
    real = mutate._measure
    monkeypatch.setattr(mutate, "_measure", lambda *a: real(*a)._replace(
        via="cache"))
    written = []
    real_write = mutate._write
    monkeypatch.setattr(mutate, "_write", lambda path, text, when: (
        written.append(text), real_write(path, text, when)))
    run = Campaign(tree, monkeypatch, said=lambda r: killed_by(MINOR)
                   if r.text else GREEN)
    report = run(2)
    assert report["killed"] == report["mutants"] == 6
    # Three baselines and the six mutants: no copy restored, no run after.
    assert len(run.runs) == 3 + 6
    assert run.source not in written


def test_a_run_cut_short_is_never_a_verdict(tmp_path):
    """Once the campaign is stopping, a run is stopped with everything it
    started, and says `Stopped` rather than a verdict of a run that never
    finished."""
    beat = tmp_path / "beat"
    (tmp_path / "test_hang.py").write_text(
        "import os, subprocess, sys, time\n"
        "\n"
        "BEAT = %r\n"
        "\n"
        "def test_hang():\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time\\n'\n"
        "                      'while True:\\n'\n"
        "                      '    open(%%r, \"a\").write(\".\")\\n'\n"
        "                      '    time.sleep(0.05)' %% BEAT])\n"
        "    time.sleep(60)\n" % str(beat), encoding="utf-8")
    stop = threading.Event()
    threading.Thread(target=lambda: (wait_for(str(beat)), stop.set())).start()

    started = time.monotonic()
    with pytest.raises(mutate.Stopped):
        mutate.run_tests(str(tmp_path), ["test_hang.py"], 60, stop=stop)
    assert time.monotonic() - started < 30
    before = beat.stat().st_size
    time.sleep(0.5)
    assert beat.stat().st_size == before, "a process the run started lives on"


def test_a_run_is_not_started_once_the_campaign_is_stopping(monkeypatch):
    started = []
    monkeypatch.setattr(mutate.subprocess, "Popen",
                        lambda *a, **k: started.append(a))
    stop = threading.Event()
    stop.set()
    with pytest.raises(mutate.Stopped):
        mutate.run_tests("/nowhere", ["t.py"], 1, stop=stop)
    assert started == []


def test_a_stop_at_one_worker_lands_what_ended_and_not_what_was_cut_short(
        tree, monkeypatch, landed):
    """A ^C during the second mutant's run: the first mutant's kill stands,
    and the second's run, cut short, is no outcome."""
    def said(run):
        if run.text is None:
            return GREEN
        if len([r for r in campaign.runs if r.text]) == 1:
            return killed_by(MINOR)
        _thread.interrupt_main()
        assert campaign.stop.wait(30)
        raise mutate.Stopped

    campaign = Campaign(tree, monkeypatch, said=said)
    with pytest.raises(KeyboardInterrupt):
        campaign(1)
    assert [n for n, _o in landed] == [1]
    assert not any(ln.startswith("stopping") for ln in campaign.lines)
    assert not any(os.path.exists(where) for where in campaign.places)


def test_a_stop_at_three_workers_lands_survivors_and_kills_read_back_only(
        tree, monkeypatch, landed):
    """Three runs end as the workers are being stopped, after a ^C: the
    survivor stands, and so does the kill read back, but the kill not yet
    confirmed does not."""
    real = mutate._measure

    def measure(mutant, copy, attempts, budget):
        got = real(mutant, copy, attempts, budget)
        return got._replace(via="cache") if mutant.job.n == 2 else got

    monkeypatch.setattr(mutate, "_measure", measure)
    interrupted = threading.Event()

    def said(run):
        if run.text is None:
            return GREEN
        if not interrupted.is_set():
            interrupted.set()
            _thread.interrupt_main()
        assert campaign.stop.wait(30)
        return (GREEN, killed_by(MINOR), killed_by(ADULT))[run.copy]

    campaign = Campaign(tree, monkeypatch, said=said)
    with pytest.raises(KeyboardInterrupt):
        campaign(3)
    # The first three mutants went to the three copies in turn.
    assert sorted((n, o.verdict.ok, o.via) for n, o in landed) == [
        (1, True, ""), (2, False, "cache")]
    assert [ln for ln in campaign.lines if ln.startswith("stopping")] == [
        "stopping 3 run(s)"]
    assert not any(os.path.exists(where) for where in campaign.places)


def test_a_worker_that_ends_saying_nothing_is_a_refusal(tree, monkeypatch):
    """Not a wait for ever: the campaign runs in a thread of the test's,
    which gives it five seconds."""
    monkeypatch.setattr(mutate._Pool, "_work", lambda self, k: None)
    run = Campaign(tree, monkeypatch)
    raised = []

    def campaign():
        try:
            run(2)
        except BaseException as exc:
            raised.append(exc)

    thread = threading.Thread(target=campaign, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "the campaign still waits for the worker"
    (exc,) = raised
    assert isinstance(exc, mutate.Refusal)
    assert "a worker ended without saying" in str(exc)


def test_a_worker_that_ends_saying_why_is_heard(tree, monkeypatch):
    """What it posted is read before its end is: the error, not the end."""
    def work(self, k):
        self.inboxes[k].get()
        self.posts.put((k, k, OSError("the disk is full")))

    monkeypatch.setattr(mutate._Pool, "_work", work)
    run = Campaign(tree, monkeypatch)
    with pytest.raises(OSError, match="the disk is full"):
        run(2)


def _copies(tmp_path, count, stop=None):
    copies = []
    for k in range(count):
        where = tmp_path / ("c%d" % k)
        write_tree(str(where), {"m.py": "x = 1\n"})
        copies.append(mutate.Copy(str(where), "m.py", "x = 1\n", ["t.py"],
                                  None, (), stop or threading.Event(), 2))
    return copies


def test_the_pool_gives_each_its_results_in_order(tmp_path):
    copies = _copies(tmp_path, 3)
    pool = mutate._Pool(copies, threading.Event(), print)
    try:
        assert pool.each(lambda copy: copy.where) == [c.where for c in copies]

        def slow_first(copy, item):
            time.sleep(0.3 if item == 0 else 0)
            return item * 10, copy.where

        got = pool.map(slow_first, range(5))
        assert [item for item, _where in got] == [0, 10, 20, 30, 40]
        assert len({where for _item, where in got}) > 1
        with pytest.raises(ZeroDivisionError):
            pool.map(lambda copy, item: 1 / item, [1, 0])
    finally:
        pool.close()
    assert not any(thread.is_alive() for thread in pool.threads)


def test_a_stop_during_the_halt_goes_on_once_every_worker_has_ended(
        tmp_path):
    """A second ^C while the workers are being stopped would otherwise
    leave a run going in a copy about to be removed."""
    copies = _copies(tmp_path, 1)
    stop = threading.Event()
    pool = mutate._Pool(copies, stop, print)
    ended = []

    def task(copy):
        stop.wait(10)
        _thread.interrupt_main()
        time.sleep(0.5)
        ended.append(True)
        return 1

    pool.give(0, 0, task)
    with pytest.raises(KeyboardInterrupt):
        pool.halt()
    assert ended == [True]
    assert not pool.threads[0].is_alive()
    assert pool.left == [(0, 0, 1)]


def test_a_copy_holding_a_mutant_is_never_run_as_the_original(
        tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(mutate, "run_tests", lambda *a: runs.append(a) or GREEN)
    (copy,) = _copies(tmp_path, 1)
    copy.clock = 100
    mutant = mutate.Mutant(mutate.Job(2, 0, "CONST", "1 -> 2", 1), "x = 2\n",
                           True)
    copy.run(None, 1)
    copy.run(mutant, 1)
    copy.run(mutant, 1)
    with pytest.raises(RuntimeError):
        copy.run(None, 1)
    assert len(runs) == 3
    copy.restore()
    copy.run(None, 1)
    with open(copy.path, encoding="utf-8") as fh:
        assert fh.read() == "x = 1\n"
    # The pool's stamp was the clock plus the mutant's place; the original
    # back takes the next second past the last place.
    assert os.stat(copy.path).st_mtime == 100 + 2 + 1


# --------------------------------------------------------------------------
# An outcome


def _mutant():
    return mutate.Mutant(mutate.Job(1, 0, "RAISE", "raise ... -> pass", 3),
                         "text", True)


class _Copy:
    """A copy whose every run of the whole selection says *verdict*."""

    rel = GATE

    def __init__(self, verdict):
        self.verdict, self.runs = verdict, []

    def run(self, mutant, timeout, selection=None, **handshake):
        self.runs.append(selection)
        return self.verdict


def test_an_attempt_that_says_nothing_leaves_the_mutant_to_the_selection():
    copy = _Copy(killed_by(MINOR))
    attempts = [lambda mutant, copy_: None,
                lambda mutant, copy_: None]
    assert mutate._measure(_mutant(), copy, attempts, 30) == mutate.Outcome(
        killed_by(MINOR))
    assert copy.runs == [None]


def test_the_first_attempt_that_decides_is_the_outcome():
    copy = _Copy(GREEN)
    probe = mutate.Outcome(killed_by(MINOR), via="probe")
    attempts = [lambda m, c: None, lambda m, c: probe,
                lambda m, c: pytest.fail("asked after a decision")]
    assert mutate._measure(_mutant(), copy, attempts, 30) is probe
    assert copy.runs == []


@pytest.mark.parametrize("via, survives", [
    ("", True), ("cache", True), ("probe", False), ("coverage", False)])
def test_a_survivor_is_reached_only_by_the_whole_selection(via, survives):
    """An attempt that runs fewer tests than the selection can kill a
    mutant, never let one through."""
    outcome = mutate.Outcome(GREEN, via=via)
    attempts = [lambda m, c: outcome]
    if survives:
        assert mutate._measure(_mutant(), _Copy(GREEN), attempts, 30) is outcome
    else:
        with pytest.raises(ValueError):
            mutate._measure(_mutant(), _Copy(GREEN), attempts, 30)


@pytest.mark.parametrize("code", [ExitCode.USAGE_ERROR,
                                  ExitCode.NO_TESTS_COLLECTED])
def test_a_run_of_the_whole_selection_that_collects_nothing_is_refused(code):
    """An attempt's run that collects nothing is its own to set aside; the
    whole selection's is the harness, and refused."""
    attempts = [lambda m, c: None]
    with pytest.raises(mutate.Refusal) as caught:
        mutate._measure(_mutant(), _Copy(mutate.Verdict(False, code, "", "")),
                        attempts, 30)
    assert "pkg%sgate.py:3 raise ... -> pass made pytest exit %d" % (
        os.sep, code) in str(caught.value)
    # A module that would not import is named, and is a kill.
    named = mutate.Verdict(False, code, "", "pkg/tests/test_gate.py")
    assert mutate._measure(_mutant(), _Copy(named), [], 30).verdict == named


def test_a_gate_runs_the_file_s_tests_on_the_original(tmp_path, monkeypatch):
    runs = []
    monkeypatch.setattr(mutate, "run_tests", lambda *a: runs.append(a) or GREEN)
    (copy,) = _copies(tmp_path, 1)
    got, took = mutate._gate(copy, "one.txt", 7)
    assert got == GREEN and took >= 0
    assert runs[0][2:4] == (7, "one.txt")


@pytest.mark.parametrize("tests, plain", [
    (["pkg/tests"], True), (GATE_TESTS, True),
    (["pkg/tests/test_gate.py::test_a_minor_is_refused"], True),
    (["pkg/tests", "-k", "minor"], False), (["pkg/tests/nothere.py"], False),
    (["::x"], False)])
def test_plain_tests_are_paths_or_node_ids_that_are_there(tree, tests, plain):
    assert mutate._plain_tests(tests, tree) is plain


# --------------------------------------------------------------------------
# Confirmation, and the copy it is made in


def test_a_confirmation_that_collects_nothing_is_refused(tree, monkeypatch,
                                                        landed):
    """The whole selection run again on a copy the mutants left broken says
    pytest's 5 with no killer: that is the harness, and never a kill."""
    def said(run):
        if run.text is None:
            return GREEN
        if run.copy == 0 and len([r for r in campaign.runs
                                  if r.text is None]) > 3:
            return mutate.Verdict(False, ExitCode.NO_TESTS_COLLECTED, "", "")
        return mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT")

    campaign = Campaign(tree, monkeypatch, said=said)
    with pytest.raises(mutate.Refusal) as caught:
        campaign(2, only=["RAISE"])
    assert "made pytest exit 5" in str(caught.value)
    assert landed == []


@pytest.mark.parametrize("killer, gate", [(MINOR, False),
                                          ("pkg/tests/test_gate.py", None)])
def test_a_first_copy_red_on_the_original_after_the_mutants_is_refused(
        tree, monkeypatch, landed, killer, gate):
    """Once the original is back in the first copy, the whole selection
    runs there before any kill is confirmed: a run that left something a
    test reads would make every confirmation a kill. Whether the killer
    could be run alone or not, nothing is confirmed."""
    restored = []
    real = mutate.Copy.restore
    monkeypatch.setattr(mutate.Copy, "restore", lambda self: (
        restored.append(self), real(self)))

    def said(run):
        if run.text is not None:
            return killed_by(killer)
        if restored and not run.alone:
            return killed_by(MINOR)
        if restored:
            return killed_by(MINOR) if gate is False else GREEN
        return GREEN

    campaign = Campaign(tree, monkeypatch, said=said)
    with pytest.raises(mutate.Refusal) as caught:
        campaign(2)
    assert "not green in the first copy once the original" in str(caught.value)
    assert [o for _n, o in landed if not o.verdict.ok] == []
    after = campaign.runs[campaign.runs.index(next(
        r for r in campaign.runs[3:] if r.text is None)):]
    assert after == [Run(0, None, "", after[0].timeout)]


def test_a_kill_with_no_killer_to_run_alone_is_decided_by_the_selection_again(
        tree, monkeypatch, landed):
    """A green first copy, and a kill whose killer is a module: the whole
    selection again decides it, and it lands then."""
    restored, again = [], []
    real = mutate.Copy.restore
    monkeypatch.setattr(mutate.Copy, "restore", lambda self: (
        restored.append(self), real(self)))

    def said(run):
        if run.text is None:
            return GREEN
        if restored and not again:
            # The first mutant run once the original was back: a survivor.
            again.append(run)
            return GREEN
        return killed_by("pkg/tests/test_gate.py", ExitCode.INTERRUPTED)

    campaign = Campaign(tree, monkeypatch, said=said)
    report = campaign(2, only=["RAISE", "BOOL"])
    assert [s.get("confirmed") for s in report["survivors"]] == ["full"]
    assert [k["confirmed"] for k in report["kills"]] == ["full"]
    assert not [r for r in campaign.runs if r.alone]
    assert len(landed) == 2


# --------------------------------------------------------------------------
# Real runs


#: A gate with two refusals, and a test that hangs on either's removal
#: after writing its pid and its parent's to a file of its own in *box*;
#: on the original both refusals fire and it returns at once.
TWO_REFUSALS = (
    "def admit(age):\n"
    "    if age < 18:\n"
    "        raise ValueError('under age')\n"
    "    if age > 150:\n"
    "        raise ValueError('too old')\n"
    "    return age\n")

TWO_HANGS = (
    "import os\n"
    "import time\n"
    "from pkg import gate\n"
    "\n"
    "def test_slow():\n"
    "    for age in (10, 200):\n"
    "        try:\n"
    "            gate.admit(age)\n"
    "        except ValueError:\n"
    "            continue\n"
    "        with open(os.path.join(%r, str(os.getpid())), 'w') as fh:\n"
    "            fh.write(str(os.getpid()) + ' ' + str(os.getppid()))\n"
    "        time.sleep(60)\n")


def _two_hanging(tree, tmp_path):
    """`invective run --workers 2` on two mutants that each hang, once both
    hang: the process, the copies, and the pid of each hanging run."""
    box = tmp_path / "pids"
    box.mkdir()
    write_tree(tree, {"pkg/gate.py": TWO_REFUSALS,
                      "pkg/tests/test_slow.py": TWO_HANGS % str(box)})
    # The default ^C handler, whatever this process's own is.
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import signal, sys\n"
         "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
         "from invective.__main__ import main\n"
         "sys.exit(main())\n",
         "run", "--target", GATE, "--tests", "pkg/tests/test_slow.py",
         "--only", "RAISE", "--workers", "2"],
        cwd=tree, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": SRC})
    places = [proc.stdout.readline(), proc.stdout.readline()]
    assert all(line.startswith("copy:") for line in places), (
        places, proc.communicate(timeout=30))
    end = time.monotonic() + 60
    while len(os.listdir(box)) < 2 and time.monotonic() < end:
        time.sleep(0.05)
    runs = [int(name) for name in os.listdir(box)]
    assert len(runs) == 2, runs
    return proc, [line.split(None, 1)[1].strip() for line in places], runs


@pytest.mark.skipif(os.name == "nt", reason="Windows never delivers SIGTERM")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_stopped_run_with_two_workers_stops_both_runs_and_removes_both_copies(
        tree, tmp_path, signum):
    """Both runs hang, each in a group of its own that the signal never
    reaches: the engine stops both, says so once, removes both copies, and
    goes on by the signal."""
    proc, places, runs = _two_hanging(tree, tmp_path)
    try:
        os.kill(proc.pid, signum)
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == -signum, err
        assert out.count("stopping") == 1
        assert "stopping 2 run(s)" in out
        assert not any(os.path.exists(where) for where in places)
        for run in runs:
            with pytest.raises(ProcessLookupError):
                os.killpg(run, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        for run in runs:
            stop_group(run)


def test_a_real_campaign_with_two_workers_confirms_its_kill_alone(
        tree, tmp_path, monkeypatch, capsys):
    """The fixture's one refusal, checked by a test: killed in the pool,
    then by that test alone with nothing else running."""
    monkeypatch.chdir(tree)
    out = tmp_path / "r.json"
    assert mutate.main(["--target", os.path.join(tree, GATE), "--tests",
                        os.path.join(tree, "pkg", "tests", "test_gate.py"),
                        "--only", "RAISE,BOOL", "--workers", "2",
                        "--json", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["workers"] == 2
    assert [(k["killer"], k["confirmed"]) for k in report["kills"]] == [
        (MINOR, "alone")]
    assert [s["line"] for s in report["survivors"]] == [4]
    said = capsys.readouterr().out
    assert said.count("copy:") == 2 and "workers:   2" in said


@pytest.mark.parametrize("given", ["0", "-1", "autos", "two"])
def test_the_command_refuses_a_count_of_workers_by_name(tree, monkeypatch,
                                                       capsys, given):
    monkeypatch.chdir(tree)
    assert mutate.main(["--target", os.path.join(tree, GATE), "--tests",
                        os.path.join(tree, "pkg", "tests", "test_gate.py"),
                        "--workers", given]) == 2
    said = capsys.readouterr()
    assert ("refused: --workers must be a whole number above 0 or \"auto\", "
            "not %r" % given) in said.err
    assert "copy:" not in said.out


def test_the_setting_is_the_default_and_the_flag_wins(tree, monkeypatch):
    write_tree(tree, {"pyproject.toml": (
        "[tool.pytest.ini_options]\n[tool.invective]\nworkers = 2\n")})
    given = []
    monkeypatch.setattr(mutate, "mutate", lambda *a, **k: given.append(
        k["workers"]) or {"killed": 0, "mutants": 1, "survivors": [],
                          "accepted": [], "stale": [], "kills": [],
                          "broken": 0})
    monkeypatch.chdir(tree)
    args = ["--target", GATE, "--tests", "pkg/tests/test_gate.py"]
    mutate.main(args)
    mutate.main(args + ["--workers", "auto"])
    assert given == [2, "auto"]
