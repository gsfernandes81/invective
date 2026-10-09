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
import itertools
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import warnings
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from pytest import ExitCode

import pytest_invective
from invective import config, mutate, process

from conftest import FILES, SRC, commit, git, stop_group, wait_for, write_tree
from test_mutate import ACCEPTING, NESTED

GATE = os.path.join("pkg", "gate.py")
GATE_FILE = "pkg/gate.py"
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
    # Pinned with `TIMED_OUT` at -1. Its value is any code pytest cannot
    # exit with (its acceptance says so), and a kill by time is pinned as
    # `TIMED_OUT`, whatever that is.
    kept["kills"] = [{**kill, "code": -1} if kill["code"] == mutate.TIMED_OUT
                     else kill for kill in kept["kills"]]
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
def test_three_workers_report_as_one_does(tree, monkeypatch, name, landed):
    """The same entries in the same order, from runs that ended in another
    order, and every kill final as it ends: nothing is run once the last
    mutant has, and nothing says how a kill was confirmed."""
    one = Campaign(tree, monkeypatch, TARGETS[name], by_text)(1)
    run = Campaign(tree, monkeypatch, TARGETS[name], by_text)
    three = run(3)

    assert (one["workers"], three["workers"]) == (1, 3)
    assert {**three, "workers": 1} == one
    assert "unreproduced" not in three
    # Three baselines alone and at once, and each mutant once.
    assert len(run.runs) == 1 + 3 + three["mutants"]
    assert len(landed) == 2 * three["mutants"]


@pytest.mark.parametrize("name", sorted(TARGETS))
def test_three_workers_confirming_report_as_one_does_but_for_how(
        tree, monkeypatch, name):
    """Asked to confirm: each kill carries how it was confirmed, and nothing
    else differs. A killer that is a test failed alone, and the rest were
    the whole selection again; none is named as not coming back alone."""
    one = Campaign(tree, monkeypatch, TARGETS[name], by_text)(1)
    three = Campaign(tree, monkeypatch, TARGETS[name], by_text)(3,
                                                               confirm=True)

    assert (one["workers"], three["workers"]) == (1, 3)
    assert three.pop("unreproduced") == []
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
    """One worker, a line after every tenth, as a serial campaign says it:
    the count, and how many of them survived."""
    run = Campaign(tree, monkeypatch, TARGETS["many"], said=lambda r: (
        killed_by(MINOR) if r.text and "[1, 1," in r.text else GREEN))
    run(1, only=["CONST"])
    assert run.lines[:2] == ["copy:      %s" % run.places[0], "workers:   1"]
    assert [ln for ln in run.lines if ln.startswith("  ...")] == [
        "  ... 10/12, 9 survived"]


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


@pytest.mark.parametrize("given", [8, "8"])
def test_a_count_of_workers_is_at_most_one_a_mutant(tree, monkeypatch, given):
    """A copy more than there are mutants would run a baseline beside the
    others and no mutant, a load that raises the budget for nothing."""
    run = Campaign(tree, monkeypatch, TARGETS["many"])
    assert run(given, limit=2)["workers"] == 2
    assert len(run.places) == 2 and "workers:   2" in run.lines
    assert config.workers(given, most=12) == 8


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
    report = campaign(2, only=["RAISE", "BOOL"])
    assert report["workers"] == 2
    assert report["survivors"] and not report["kills"]


def test_a_copy_red_or_short_on_the_unmutated_tree_is_refused(
        tree, monkeypatch):
    """Each copy runs the selection at once with the others, and each must
    be green on its own: a test red only beside another copy's run, or a
    copy missing a test, would score kills that are no mutant's."""
    for wrong, says in [(killed_by(MINOR), "green alone and RED in 1 of 3 "
                         "copies running it at once"),
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
        if wrong is not GREEN and not wrong.ok:
            # What the red copy's run said, not a green one's.
            assert str(caught.value).endswith("\n1 failed")
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


@pytest.mark.parametrize("workers", [1, 2, 3])
def test_every_copy_of_a_ref_is_the_commit_the_first_copy_holds(
        repo, monkeypatch, workers):
    """The first copy is a worktree of the ref; the others are of the
    commit it holds, and not of the ref again, which can name another
    commit by then. With one worker, git is asked nothing more than a
    serial campaign asks it."""
    before = git(repo, "rev-parse", "HEAD").strip()
    made, asked = [], []
    real = mutate.git_ref
    real_commit = mutate.commit_of
    monkeypatch.setattr(mutate, "commit_of", lambda where: (
        asked.append(where), real_commit(where))[1])

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
    report = run(workers, ref="main", only=["RAISE", "BOOL", "CMP"])
    assert report["workers"] == workers
    assert made == ["main"] + [before] * (workers - 1)
    assert len(asked) == (workers > 1)
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
        if run.text is None or "raise" in run.text:
            return GREEN
        return killed_by("pkg/tests/test_gate.py")

    run = Campaign(tree, monkeypatch, said=said)
    report = run(2, only=["RAISE", "BOOL"], confirm=True)

    (kill,) = report["kills"]
    assert kill["confirmed"] == "full"
    # Three baselines, the two mutants' runs in the pool, then the first
    # copy restored and run, and the mutant killed run there again.
    (pooled,) = [r for r in run.runs[3:5] if "raise" not in r.text]
    guard, again = run.runs[5:]
    assert (guard.text, again.text) == (None, pooled.text)
    # Each budget is some multiple of a baseline, and any well above it
    # serves (their acceptances say so), so the multiple is not pinned:
    # what is pinned is which baseline each is measured from, and that a
    # confirmation never gets less than the loaded budget.
    assert guard.timeout == again.timeout >= pooled.timeout
    if alone:
        assert pooled.timeout == 30.0
        assert guard.timeout >= 3 * alone
    else:
        assert pooled.timeout >= 3 * together
        assert guard.timeout == pooled.timeout


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
    report = run(3, confirm=True)
    # Each mutant once in the pool, and each kill once more to confirm it.
    assert len(threads) == report["mutants"] + report["killed"]
    assert set(threads) == {threading.main_thread()}


def test_a_worker_waiting_on_its_run_lets_a_text_be_made(tmp_path):
    """Out of the hush while it waits on its run, and back in only once the
    text being made is done: the texts are made while the runs go on."""
    hush = mutate._Hush()
    waiting, done, back = threading.Event(), threading.Event(), []

    def worker():
        with hush.shared():
            with mutate._Hush.aside():
                waiting.set()
                assert done.wait(10)
            back.append(list(made))

    def text():
        with hush.alone():
            made.append("text")
            done.set()
            time.sleep(0.3)
            made.append("after")

    made = []
    workers = threading.Thread(target=worker, daemon=True)
    workers.start()
    assert waiting.wait(10)
    texts = threading.Thread(target=text, daemon=True)
    texts.start()
    texts.join(5)
    workers.join(5)
    assert not texts.is_alive(), "the text waited for a worker's run"
    # The worker came back once the text was made, not halfway through.
    assert back == [["text", "after"]]


def test_an_exception_as_a_text_s_turn_begins_leaves_the_workers_free():
    """An exception as `alone` lets its lock go, once it is the text's
    turn, ends the text before it is made, and does not leave every worker
    held off for ever."""
    hush = mutate._Hush()
    raised = []

    class Turn(threading.Condition):
        def __exit__(self, *exc):
            super().__exit__(*exc)
            if hush._alone and not raised:
                raised.append(True)
                raise KeyboardInterrupt

    hush._turn = Turn()
    with pytest.raises(KeyboardInterrupt):
        with hush.alone():
            pytest.fail("the text was made")
    assert raised and not hush._alone
    entered = threading.Event()

    def worker():
        with hush.shared():
            entered.set()

    threading.Thread(target=worker, daemon=True).start()
    assert entered.wait(5), "a worker is held off for ever"


def test_a_worker_held_off_gives_up_once_the_campaign_is_stopping():
    """While a text is being made, a worker that would start its task, or
    come back from waiting on its run, waits; once the campaign is stopping
    it gives up, raising `Stopped`, so a halt never waits on a text. What
    the hush counts is still right once the text is done."""
    stop = threading.Event()
    hush = mutate._Hush(stop)
    got = {}
    aside, back, inside, done = (threading.Event() for _ in range(4))

    def first():
        try:
            with hush.shared():
                with mutate._Hush.aside():
                    aside.set()
                    assert back.wait(10)
                got["first"] = "back in"
        except mutate.Stopped:
            got["first"] = "stopped"

    def second():
        try:
            with hush.shared():
                got["second"] = "in"
        except mutate.Stopped:
            got["second"] = "stopped"

    def text():
        with hush.alone():
            inside.set()
            assert done.wait(10)

    threads = [threading.Thread(target=first, daemon=True)]
    threads[0].start()
    try:
        assert aside.wait(10)
        threads.append(threading.Thread(target=text, daemon=True))
        threads[-1].start()
        assert inside.wait(10)
        back.set()
        threads.append(threading.Thread(target=second, daemon=True))
        threads[-1].start()
        time.sleep(0.5)
        assert got == {}, "a worker went on while a text was made"
        stop.set()
        for thread in (threads[0], threads[2]):
            thread.join(5)
        assert got == {"first": "stopped", "second": "stopped"}
    finally:
        back.set()
        done.set()
        for thread in threads:
            thread.join(5)
    assert (hush._sharing, hush._alone, hush._asking) == (0, False, 0)


#: A campaign, run in a process of its own, with a ^C landing on the main
#: thread at the start of a lock's `__exit__`, before the release, where
#: 3.12 and later can land one: the handler is called there, as Python calls
#: it. Each run waits out of the hush, as a real one does, each copy's for
#: a time of its own so that they do not end together, and says green.
#: `hush` lands it as a text's turn begins while a run goes on, and `queue`
#: as the main thread reads a post.
LANDING = """\
import os, signal, sys, threading, time
from invective import mutate

top, at = sys.argv[1:]
fired, running, copies = [], [], []


class Turn(threading.Condition):
    def __init__(self, lock=None, when=lambda: True):
        super().__init__(lock)
        self.when = when

    def __exit__(self, *exc):
        if (not fired and threading.current_thread() is threading.main_thread()
                and self.when()):
            fired.append(True)
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        return super().__exit__(*exc)


if at == "hush":
    real_hush = mutate._Hush.__init__

    def hush(self, *args):
        real_hush(self, *args)
        self._turn = Turn(when=lambda: self._alone and running)

    mutate._Hush.__init__ = hush
else:
    real_pool = mutate._Pool.__init__

    def pool(self, *args):
        real_pool(self, *args)
        self.posts.not_empty = Turn(self.posts.mutex)

    mutate._Pool.__init__ = pool


def run_tests(where, tests, timeout, selection=None, options=(), target="",
              stop=None):
    if where not in copies:
        copies.append(where)
    running.append(where)
    with mutate._Hush.aside():
        time.sleep(0.1 + 0.15 * copies.index(where))
    running.remove(where)
    return mutate.Verdict(True, 0, "", "")


mutate.run_tests = run_tests
said = []
try:
    mutate.mutate(top, os.path.join(top, "pkg", "gate.py"),
                  ["pkg/tests/test_gate.py"], None, None, say=said.append,
                  workers=2)
except KeyboardInterrupt:
    print("interrupted")
print("landed" if fired else "never landed")
print("left" if any(os.path.exists(line.split(None, 1)[1]) for line in said
                    if line.startswith("copy:")) else "removed")
"""


@pytest.mark.parametrize("at", ["hush", "queue"])
def test_a_stop_landing_as_a_lock_is_let_go_is_raised_with_none_held(
        tree, at):
    """Raised where it lands, the ^C would leave the lock held, and the halt
    would wait for ever on a worker that needs it. Held while the workers
    run, it is raised where the main thread waits, with no lock held: the
    campaign stops, and its copies are removed."""
    # Twelve mutants, for texts made while runs go on.
    write_tree(tree, {GATE_FILE: TARGETS["many"]})
    try:
        done = subprocess.run([sys.executable, "-c", LANDING, tree, at],
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PYTHONPATH": SRC})
    except subprocess.TimeoutExpired:
        pytest.fail("the campaign waited for ever on a lock")
    assert done.stdout.split() == ["interrupted", "landed", "removed"], (
        done.stdout, done.stderr)


def test_a_worker_s_real_run_is_waited_on_out_of_the_hush(tmp_path):
    """A text is made while a worker's run goes on: waiting on its process,
    the worker is out of the hush."""
    started = tmp_path / "started"
    (tmp_path / "test_slow.py").write_text(
        "import pathlib, time\n"
        "\n"
        "def test_slow():\n"
        "    pathlib.Path(%r).write_text('x')\n"
        "    time.sleep(5)\n" % str(started), encoding="utf-8")
    hush, stop = mutate._Hush(), threading.Event()

    def worker():
        with hush.shared():
            try:
                mutate.run_tests(str(tmp_path), ["test_slow.py"], 60,
                                 stop=stop)
            except mutate.Stopped:
                pass

    workers = threading.Thread(target=worker, daemon=True)
    workers.start()
    try:
        wait_for(str(started))
        made = []

        def text():
            with hush.alone():
                made.append(True)

        texts = threading.Thread(target=text, daemon=True)
        texts.start()
        texts.join(3)
        assert made == [True], "the text waited for the run to end"
    finally:
        stop.set()
        workers.join(30)


def test_a_worker_stops_its_timed_out_run_out_of_the_hush(tmp_path,
                                                        monkeypatch):
    """Stopping a run that ran out of time can take `process.STOP_GRACE`,
    when something it started holds its output: a text is made meanwhile."""
    stopping, done = threading.Event(), threading.Event()
    real = mutate.process.stop

    def stop(proc):
        stopping.set()
        assert done.wait(10)
        real(proc)

    def wait(proc, timeout, stop):
        raise subprocess.TimeoutExpired("pytest", timeout)

    monkeypatch.setattr(mutate.process, "stop", stop)
    monkeypatch.setattr(mutate, "_wait", wait)
    hush, got = mutate._Hush(threading.Event()), []

    def worker():
        with hush.shared():
            got.append(mutate.run_tests(str(tmp_path), ["t.py"], 1,
                                        stop=threading.Event()))

    workers = threading.Thread(target=worker, daemon=True)
    workers.start()
    try:
        assert stopping.wait(10)
        made = []

        def text():
            with hush.alone():
                made.append(True)

        texts = threading.Thread(target=text, daemon=True)
        texts.start()
        texts.join(3)
        assert made == [True], "the text waited for the run to be stopped"
    finally:
        done.set()
        workers.join(30)
    assert [verdict.code for verdict in got] == [mutate.TIMED_OUT]


#: How many warnings each run gives.
WARNINGS = 40


def test_no_worker_warns_while_a_text_is_made(tree, monkeypatch):
    """A text's parse sets the process's warning filters aside, and a
    warning a worker gives then would be lost, ignored. Every run here warns
    over and over while the texts are made, slowly: every warning is
    heard."""
    real = mutate.ast.parse

    def parse(*args, **kwargs):
        time.sleep(0.01)
        return real(*args, **kwargs)

    def said(run):
        if run.text is not None:
            for _ in range(WARNINGS):
                warnings.warn("from a run", UserWarning)
                time.sleep(0.002)
        return GREEN

    run = Campaign(tree, monkeypatch, TARGETS["many"], said)
    monkeypatch.setattr(mutate.ast, "parse", parse)
    with warnings.catch_warnings(record=True) as heard:
        warnings.simplefilter("always")
        report = run(3)
    assert report["mutants"] == 12
    runs = len([r for r in run.runs if r.text is not None])
    assert len([w for w in heard if str(w.message) == "from a run"]) == (
        WARNINGS * runs)


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
    report = run(2, confirm=True)
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
    report = run(2, confirm=True)

    assert report["kills"] == []
    confirmed = [s for s in report["survivors"] if "confirmed" in s]
    assert confirmed and all(s["confirmed"] == "full" for s in confirmed)
    # Alone on the original, then alone on each mutant, then the whole
    # selection on each.
    alone = [r for r in run.runs if r.alone]
    assert alone[0] == Run(0, None, MINOR, alone[0].timeout)
    assert len(alone) == 1 + len(confirmed)
    assert len(landed) == report["mutants"]
    # Each named, with its killer, which is the test to look at.
    assert [(u["line"], u["change"], u["killer"], u["alone"], u["again"])
            for u in report["unreproduced"]] == [
        (s["line"], s["change"], MINOR, "passes alone on the mutant",
         "survived") for s in confirmed]


@pytest.mark.parametrize("tests, gated", [
    (GATE_TESTS, True), (GATE_TESTS + ["-k", "minor or adult"], False)])
def test_a_killer_is_run_alone_only_where_it_leaves_out_nothing_the_tests_chose(
        tree, monkeypatch, tests, gated):
    """On the command line, `--tests` can hold an option a run of one test
    would drop with them, so then the kill is confirmed by the whole
    selection."""
    run = Campaign(tree, monkeypatch, said=lambda r: killed_by(MINOR)
                   if r.text and "raise" not in r.text else GREEN)
    report = run(2, only=["RAISE", "BOOL"], tests=tests, confirm=True)
    (kill,) = report["kills"]
    assert kill["confirmed"] == ("alone" if gated else "full")
    assert bool([r for r in run.runs if r.alone]) is gated
    # A killer never run alone is not one that did not come back alone.
    assert report["unreproduced"] == []


@pytest.mark.parametrize("kill, tests", [
    (mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT"),
     GATE_TESTS),
    (killed_by("pkg/tests/test_gate.py", ExitCode.INTERRUPTED), GATE_TESTS),
    (killed_by(MINOR), GATE_TESTS + ["-k", "minor or adult"])])
def test_a_kill_with_no_killer_to_run_alone_that_the_selection_lets_through_is_named(
        tree, monkeypatch, kill, tests):
    """A kill by time, by a module, or by a test that cannot be run alone
    is the whole selection's again, and when that lets the mutant through
    it did not come back: it is named, as a kill its killer does not make
    alone is, and the survivor says how it was decided."""
    def said(run):
        if run.text is None:
            return GREEN
        # In the pool, on the second copy only: with nothing else running,
        # every mutant passes.
        return kill if run.copy == 1 else GREEN

    run = Campaign(tree, monkeypatch, said=said)
    report = run(2, tests=tests, confirm=True)
    overturned = [s for s in report["survivors"] if "confirmed" in s]
    assert overturned and report["killed"] == 0
    assert {s["confirmed"] for s in overturned} == {"full"}
    assert [(u["line"], u["change"], u["killer"], u["alone"], u["again"])
            for u in report["unreproduced"]] == [
        (s["line"], s["change"], kill.killer, "no killer to run alone",
         "survived") for s in overturned]
    said_ = mutate.summary(report)
    assert said_[2] == (
        "             %s:%d %s  no killer to run alone (%s); the whole "
        "selection again: survived" % (GATE, overturned[0]["line"],
                                       overturned[0]["change"], kill.killer))


#: A run a signal ended, which names no test.
SIGNALLED = mutate.Verdict(False, -9, "", "")


@pytest.mark.parametrize("confirm", [False, True])
def test_a_run_a_signal_ended_is_a_kill_said_apart(tree, monkeypatch,
                                                   confirm):
    """No test failed, and no test can be run alone for it: without
    confirming it is a kill, said apart beside the kills by time; asked to
    confirm, the whole selection again decides it."""
    seen = collections.Counter()

    def said(run):
        if run.text is None:
            return GREEN
        with campaign.lock:
            seen[run.text] += 1
            again = seen[run.text] > 1
        if again:
            return killed_by(MINOR)
        if "raise" not in run.text:
            return SIGNALLED
        if "member or" in run.text:
            return GREEN
        return mutate.Verdict(False, mutate.TIMED_OUT, "TIMEOUT", "TIMEOUT")

    campaign = Campaign(tree, monkeypatch, said=said)
    report = campaign(2, only=["RAISE", "BOOL", "CMP"], confirm=confirm)
    closing = mutate.summary(report)
    (raised,) = [kill for kill in report["kills"] if kill["kind"] == "RAISE"]
    if not confirm:
        assert (raised["code"], raised["killer"]) == (-9, "")
        timeouts = [n for n, line in enumerate(closing)
                    if "stopped at their time budget" in line]
        assert closing[timeouts[0] + 1] == (
            "           1 of the kills were runs a signal or a crash ended, "
            "not a test failing")
    else:
        assert (raised["code"], raised["killer"], raised["confirmed"]) == (
            ExitCode.TESTS_FAILED, MINOR, "full")
        assert not any("a signal or a crash" in line for line in closing)


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
    report = run(2, confirm=True)
    assert {kill["confirmed"] for kill in report["kills"]} == {"full"}
    assert [r for r in run.runs if r.alone] == [
        Run(0, None, MINOR, run.runs[-1].timeout)]
    assert [(u["alone"], u["again"]) for u in report["unreproduced"]] == [
        ("fails alone on the original", "killed")] * report["killed"]


def test_two_copies_that_collide_kill_and_confirming_names_the_test(
        tree, monkeypatch, capsys):
    """`test_port` holds a port, so it fails whenever another copy runs it
    at the same moment: two workers, held at a barrier so their first runs
    meet, kill a mutant each for that, and nothing else does. Without
    confirming, the kills stand, which is what a suite that is not safe in
    parallel gets; confirming, each is run alone, comes back a survivor,
    and is named with `test_port`, in the report and in the closing
    lines."""
    port = "pkg/tests/test_gate.py::test_port"
    barrier = threading.Barrier(2, timeout=30)
    running = []

    def said(run):
        if run.text is None:
            return GREEN
        with run_.lock:
            running.append(run)
        if len([r for r in run_.runs if r.text]) <= 2:
            barrier.wait()
        try:
            return killed_by(port) if len(running) > 1 else GREEN
        finally:
            time.sleep(0.05)
            with run_.lock:
                running.remove(run)

    run_ = Campaign(tree, monkeypatch, said=said)
    plain = run_(2, only=["RAISE", "BOOL"])
    assert [k["killer"] for k in plain["kills"]] == [port, port]
    assert "unreproduced" not in plain

    barrier.reset()
    run_ = Campaign(tree, monkeypatch, said=said)
    report = run_(2, only=["RAISE", "BOOL"], confirm=True)
    assert report["kills"] == []
    assert [(u["killer"], u["alone"], u["again"])
            for u in report["unreproduced"]] == [
        (port, "passes alone on the mutant", "survived")] * 2
    said_ = mutate.summary(report)
    assert said_[1] == ("           2 kill(s) did not come back with the "
                        "killer alone, which may depend on its order or on "
                        "another copy's run:")
    assert said_[2:4] == [
        "             %s:%d %s  %s passes alone on the mutant; the whole "
        "selection again: survived" % (GATE, u["line"], u["change"], port)
        for u in report["unreproduced"]]


def test_confirming_with_one_worker_says_there_is_nothing_to_confirm(
        tree, monkeypatch):
    """Said, not refused: `confirm` in the settings is for runs with
    workers, and one worker is the serial campaign it was always."""
    run = Campaign(tree, monkeypatch, TARGETS["gate"], by_text)
    report = run(1, confirm=True)
    assert "confirm:   nothing to confirm with one worker" in run.lines
    assert _digest(report) == PINNED["gate"]
    assert "unreproduced" not in report
    two = Campaign(tree, monkeypatch)
    two(2, confirm=True)
    assert not [ln for ln in two.lines if ln.startswith("confirm:")]


def test_without_confirming_a_stop_lands_every_kill_that_ended(
        tree, monkeypatch, landed):
    """Not asked to confirm, a kill made beside other runs is final as it
    ends, as at one worker, so a stop lands it too."""
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
    assert sorted((n, o.verdict.ok) for n, o in landed) == [
        (1, True), (2, False), (3, False)]


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
    report = run(2, confirm=True)
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
    report = run(2, confirm=True)
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
        campaign(3, confirm=True)
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


class _Unheld:
    """No signal held: each raises where it lands."""

    def check(self):
        pass

    def end(self):
        pass


@pytest.mark.parametrize("held", [True, False])
def test_a_stop_during_the_halt_goes_on_once_every_worker_has_ended(
        tmp_path, monkeypatch, held):
    """A second ^C while the workers are being stopped would otherwise
    leave a run going in a copy about to be removed. Held, it is raised
    once they have ended; raised where it lands, the halt holds it until
    then."""
    if not held:
        monkeypatch.setattr(mutate, "_Held", _Unheld)
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


def test_a_post_made_as_the_halt_finds_the_queue_empty_is_kept(tmp_path):
    """The worker posts its outcome, then ends: a post made after the halt
    has read the queue empty, by a worker that has ended by the time the
    halt looks, is read all the same, and not lost with the pool."""
    (copy,) = _copies(tmp_path, 1)
    stop = copy.stop
    pool = mutate._Pool([copy], stop, print)
    drained = threading.Event()
    posts = pool.posts

    class Posts(type(posts)):
        def get_nowait(self):
            try:
                return super().get_nowait()
            except queue.Empty:
                if stop.is_set() and not drained.is_set():
                    # The worker posts and ends between this read and the
                    # halt's look at whether every worker has ended.
                    drained.set()
                    assert pool.gone[0].wait(10)
                raise

    pool.posts = Posts()

    def task(copy):
        assert stop.wait(10) and drained.wait(10)
        return "a final outcome"

    pool.give(0, 7, task)
    assert pool.halt() == [(0, 7, "a final outcome")]
    assert pool.posts.empty()


def test_a_text_held_off_by_a_worker_asks_for_a_signal_while_it_waits():
    """The main thread, waiting for its turn while a worker's task goes on,
    asks for a signal held meanwhile every spell, and makes no text until
    the worker is done."""
    order, done, inside = [], threading.Event(), threading.Event()

    def check():
        if not order:
            order.append("held off")
            done.set()

    hush = mutate._Hush(check=check)

    def worker():
        with hush.shared():
            inside.set()
            done.wait(5)

    workers = threading.Thread(target=worker, daemon=True)
    workers.start()
    assert inside.wait(5)
    with hush.alone():
        order.append("made")
    workers.join(5)
    assert order == ["held off", "made"]


def test_a_text_stopped_before_its_turn_does_not_end_another_s():
    """Its turn never came, so the turn it leaves is another's."""
    def check():
        raise KeyboardInterrupt

    hush = mutate._Hush(check=check)
    hush._alone = True
    with pytest.raises(KeyboardInterrupt):
        with hush.alone():
            pytest.fail("the text was made in another's turn")
    assert (hush._alone, hush._asking) == (True, 0)


def test_a_run_s_time_is_up_at_its_end_to_the_second(monkeypatch):
    """A run whose time is up is a `TimeoutExpired` at the moment its time
    ends, not a spell later, whatever it would have said in that spell."""
    clock = itertools.chain([0.0, 0.8], itertools.repeat(1.0))
    monkeypatch.setattr(mutate, "time", SimpleNamespace(
        monotonic=lambda: next(clock)))
    asked = []

    class Proc:
        def communicate(self, timeout):
            asked.append(timeout)
            if len(asked) == 1:
                raise subprocess.TimeoutExpired("pytest", timeout)
            return "", ""

    with pytest.raises(subprocess.TimeoutExpired):
        mutate._wait(Proc(), 1.0, threading.Event())
    assert asked == [pytest.approx(mutate._POLL)]


def test_a_halt_after_a_close_does_nothing(tmp_path):
    """The pool's end is called again as the campaign unwinds, after it
    has closed: it stops nothing and says nothing."""
    said, stop = [], threading.Event()
    pool = mutate._Pool(_copies(tmp_path, 2), stop, said.append)
    pool.close()
    assert pool.halt() == []
    assert not stop.is_set() and said == []


def test_a_halt_says_once_that_it_is_stopping_however_long_the_runs_take(
        tmp_path):
    """The halt waits on its workers in spells, and says it is stopping
    the runs in flight in the first only."""
    said, stop, release = [], threading.Event(), threading.Event()
    pool = mutate._Pool(_copies(tmp_path, 2), stop, said.append)
    gone = pool.gone[0]

    class Spells:
        """The first worker's end, which lets its task end once the halt
        has waited on it for three spells."""

        waits = 0

        def wait(self, timeout=None):
            Spells.waits += 1
            if Spells.waits == 3:
                release.set()
            return gone.wait(timeout)

        def is_set(self):
            return gone.is_set()

        def set(self):
            gone.set()

    pool.gone[0] = Spells()

    def task(copy):
        assert stop.wait(10) and release.wait(10)
        return 1

    pool.give(0, 0, task)
    assert pool.halt() == [(0, 0, 1)]
    assert Spells.waits >= 3
    assert said == ["stopping 1 run(s)"]


def test_a_pool_whose_thread_cannot_start_ends_those_started_and_holds_no_signal(
        tmp_path, monkeypatch):
    """A limit on threads or on memory: no pool is made, the worker started
    ends, and SIGINT and SIGTERM have the handlers they had."""
    handlers = [signal.getsignal(s) for s in mutate._HELD]
    real = threading.Thread.start
    started = []

    def start(thread):
        if started:
            raise RuntimeError("can't start new thread")
        started.append(thread)
        real(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        mutate._Pool(_copies(tmp_path, 2), threading.Event(), print)
    monkeypatch.undo()
    started[0].join(5)
    assert not started[0].is_alive()
    assert [signal.getsignal(s) for s in mutate._HELD] == handlers


def test_handlers_set_part_way_are_put_back(monkeypatch):
    """A handler that cannot be set leaves none of invective's behind."""
    with process.stopping_on_sigterm():
        handlers = [signal.getsignal(s) for s in mutate._HELD]
        real = signal.signal

        def install(signum, handler):
            if signum == signal.SIGTERM and handler != handlers[1]:
                raise RuntimeError("no handler for SIGTERM")
            return real(signum, handler)

        with monkeypatch.context() as patched:
            patched.setattr(signal, "signal", install)
            with pytest.raises(RuntimeError, match="no handler for SIGTERM"):
                mutate._Held()
        assert [signal.getsignal(s) for s in mutate._HELD] == handlers


def _lands(signum):
    # As the signal landing calls it: whatever handler is there.
    signal.getsignal(signum)(signum, None)


def test_a_sigterm_held_with_a_sigint_is_raised_and_not_the_sigint():
    """A supervisor's SIGTERM is not made a ^C: whichever landed first,
    the process ends by the SIGTERM. A ^C held alone is raised as one."""
    for first, second in [(signal.SIGINT, signal.SIGTERM),
                          (signal.SIGTERM, signal.SIGINT)]:
        with process.stopping_on_sigterm():
            held = mutate._Held()
            _lands(first)
            _lands(second)
            with pytest.raises(process.Terminated):
                held.end()
    held = mutate._Held()
    _lands(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt) as caught:
        held.end()
    assert not isinstance(caught.value, process.Terminated)


def test_a_sigint_held_after_a_sigterm_was_raised_is_not_raised_over_it():
    with process.stopping_on_sigterm():
        held = mutate._Held()
        _lands(signal.SIGTERM)
        with pytest.raises(process.Terminated):
            held.check()
        _lands(signal.SIGINT)
        held.end()


def test_a_stop_while_a_kill_is_confirmed_stops_the_run_going_on(
        tree, monkeypatch):
    """The campaign unwinding on a ^C halts the pool, which stops the
    confirmation's run, rather than closing it, which would wait for the
    run to end."""
    restored, stopped = [], []
    real = mutate.Copy.restore
    monkeypatch.setattr(mutate.Copy, "restore", lambda self: (
        restored.append(self), real(self)))

    def said(run):
        if run.text is not None:
            return killed_by(MINOR) if "raise" not in run.text else GREEN
        if restored and not stopped:
            _thread.interrupt_main()
            stopped.append(campaign.stop.wait(10))
        return GREEN

    campaign = Campaign(tree, monkeypatch, said=said)
    with pytest.raises(KeyboardInterrupt):
        campaign(2, only=["RAISE", "BOOL"], confirm=True)
    assert stopped == [True]


def test_a_campaign_that_loses_a_mutant_is_refused(tree, monkeypatch):
    """Two mutants that share a place, which no campaign makes: the report
    would leave one out and read as complete."""
    real = mutate.Job
    monkeypatch.setattr(mutate, "Job", lambda n, *rest: real(1, *rest))
    with pytest.raises(mutate.Refusal, match="1 of the 2 mutants came to "
                       "no verdict"):
        Campaign(tree, monkeypatch)(1, only=["RAISE", "BOOL"])


def test_one_text_is_made_ahead_for_each_run_in_flight(tree, monkeypatch):
    """With one worker, the next mutant's text is made while the first
    one's run goes on, and no more than that one."""
    made, waiting = [], threading.Event()
    real_text, real_take = mutate._text_of, mutate._Pool.take

    def text_of(module, index):
        made.append(index)
        return real_text(module, index)

    def take(self):
        # A mutant's run is in flight: the baseline's key is 0.
        if any(self.busy.values()):
            waiting.set()
        return real_take(self)

    monkeypatch.setattr(mutate, "_text_of", text_of)
    monkeypatch.setattr(mutate._Pool, "take", take)
    seen = []

    def said(run):
        if run.text is not None and not seen:
            # Out of the hush, as a real run waits.
            with mutate._Hush.aside():
                assert waiting.wait(10)
            seen.append(len(made))
        return GREEN

    Campaign(tree, monkeypatch, TARGETS["many"], said)(1, only=["CONST"])
    assert seen == [2]



def test_a_copy_writes_no_mutant_before_the_campaign_s_clock_is_set(
        tmp_path, monkeypatch):
    monkeypatch.setattr(mutate, "run_tests", lambda *a: GREEN)
    (copy,) = _copies(tmp_path, 1)
    with pytest.raises(TypeError):
        copy.run(mutate.Mutant(mutate.Job(1, 0, "CONST", "1 -> 2", 1),
                               "x = 2\n", True), 1)


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
        campaign(2, only=["RAISE", "BOOL"], confirm=True)
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
        campaign(2, confirm=True)
    assert "not green in the first copy once the original" in str(
        caught.value)
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
    report = campaign(2, only=["RAISE", "BOOL"], confirm=True)
    assert [s.get("confirmed") for s in report["survivors"]] == ["full"]
    assert [k["confirmed"] for k in report["kills"]] == ["full"]
    assert not [r for r in campaign.runs if r.alone]
    assert len(landed) == 2
    # The one the selection let through is named; the one it killed is not.
    assert [(u["line"], u["alone"], u["again"])
            for u in report["unreproduced"]] == [
        (report["survivors"][0]["line"], mutate.NO_KILLER, "survived")]


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


@pytest.mark.parametrize("workers, which, starts", [
    (1, 1, ("original", "")), (1, 2, ("mutant", "")),
    (2, 1, ("original", "")), (2, 4, ("mutant", "")),
    (2, 6, ("original", "")), (2, 7, ("original", "killer-0.txt")),
    (2, 8, ("mutant", "killer-0.txt"))])
def test_a_stop_while_a_run_starts_leaves_no_run_behind(
        tree, monkeypatch, workers, which, starts):
    """A ^C that lands inside `Popen`, once the process is made and before
    it is handed back, leaves a run no one can stop on the thread it lands
    on, which is the main thread: so no run is started there. Here one lands
    as each kind of run starts, the *which*-th: the first copy's baseline,
    a mutant's run, and with two workers each run `--confirm` makes (the
    selection on the first copy restored, the killer alone on the original,
    then on the mutant). Every run started is ended, and every copy is
    removed."""
    real = subprocess.Popen
    started, said = [], []

    def popen(*args, **kwargs):
        if pytest_invective.VERDICT not in (kwargs.get("env") or {}):
            # Not a run of the tests: on Windows, the `taskkill` that stops
            # one, which has to work for the run to be stopped and reaped.
            return real(*args, **kwargs)
        proc = real(*args, **kwargs)
        with open(os.path.join(kwargs["cwd"], GATE), encoding="utf-8") as fh:
            text = fh.read()
        listed = kwargs["env"].get(pytest_invective.SELECTION, "")
        started.append((proc, ("original" if text == FILES[GATE_FILE]
                               else "mutant", os.path.basename(listed))))
        if len(started) == which:
            _thread.interrupt_main()
            # Long enough for the ^C to land before `Popen` has returned.
            time.sleep(0.5)
        return proc

    monkeypatch.setattr(mutate.subprocess, "Popen", popen)
    try:
        with pytest.raises(KeyboardInterrupt):
            mutate.mutate(tree, os.path.join(tree, GATE), GATE_TESTS,
                          ["RAISE", "BOOL"], None, say=said.append,
                          workers=workers, confirm=True)
        # The case is the run it says it is.
        assert started[which - 1][1] == starts
        for proc, _what in started:
            # `returncode`, not `poll()`: whatever ends a run reaps it
            # (`process.stop` waits on it after the kill, `taskkill` too),
            # and `poll()` would reap a run left going that had since ended
            # on its own, and pass.
            assert proc.returncode is not None, "a run was left going"
            if os.name != "nt":
                with pytest.raises(ProcessLookupError):
                    os.killpg(proc.pid, 0)
    finally:
        for proc, _what in started:
            if proc.returncode is None:
                stop_group(proc.pid)
                proc.communicate()
    places = [line.split(None, 1)[1] for line in said
              if line.startswith("copy:")]
    assert len(places) == workers
    assert not any(os.path.exists(where) for where in places)


@pytest.mark.parametrize("confirm", [False, True])
def test_a_real_campaign_with_two_workers(tree, tmp_path, monkeypatch, capsys,
                                          confirm):
    """The fixture's one refusal, checked by a test: killed in the pool,
    and asked to, by that test alone with nothing else running."""
    monkeypatch.chdir(tree)
    out = tmp_path / "r.json"
    assert mutate.main(["--target", os.path.join(tree, GATE), "--tests",
                        os.path.join(tree, "pkg", "tests", "test_gate.py"),
                        "--only", "RAISE,BOOL", "--workers", "2",
                        "--json", str(out)]
                       + ["--confirm"] * confirm) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["workers"] == 2
    assert [(k["killer"], k.get("confirmed")) for k in report["kills"]] == [
        (MINOR, "alone" if confirm else None)]
    assert report.get("unreproduced") == ([] if confirm else None)
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


@pytest.mark.parametrize("setting", ["", "confirm = true\n"])
def test_the_setting_is_the_default_and_the_flag_wins(tree, monkeypatch,
                                                      setting):
    write_tree(tree, {"pyproject.toml": (
        "[tool.pytest.ini_options]\n[tool.invective]\nworkers = 2\n"
        + setting)})
    given = []
    monkeypatch.setattr(mutate, "mutate", lambda *a, **k: given.append(
        (k["workers"], k["confirm"])) or {
            "killed": 0, "mutants": 1, "survivors": [], "accepted": [],
            "stale": [], "kills": [], "broken": 0})
    monkeypatch.chdir(tree)
    args = ["--target", GATE, "--tests", "pkg/tests/test_gate.py"]
    mutate.main(args)
    mutate.main(args + ["--workers", "auto", "--confirm"])
    assert given == [(2, bool(setting)), ("auto", True)]
