"""`bench/compare.py`, the benchmark tool: its rules read one at a time, and
whole sessions on a tiny project.

The tool is not part of invective, so it is loaded from its file. The
sessions build two venvs each and run in seconds; they need git and uv,
which every checkout `checks` runs in has.
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib

import pytest

from conftest import commit, git

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(ROOT, "bench")


def _load():
    spec = importlib.util.spec_from_file_location(
        "bench_compare", os.path.join(BENCH, "compare.py"))
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses look their module up there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


compare = _load()


# --------------------------------------------------------------------------
# The schedule


def test_pairs_alternate_their_order_ABBA():
    """A drift in one direction favours the side that always runs later,
    unless the order alternates from one pair to the next."""
    assert [compare.order(k) for k in range(4)] == [
        ("before", "after"), ("after", "before"),
        ("before", "after"), ("after", "before")]


def _block(outcomes, pairs=3, repeats=2):
    """`run_block` against outcomes given in advance: the statuses and the
    orders each pair was asked to run in."""
    asked = []
    left = list(outcomes)

    def run_pair(k, sides):
        asked.append((k, sides))
        return left.pop(0)

    return compare.run_block(pairs, repeats, run_pair), asked


def test_a_block_runs_its_pairs_in_ABBA_order():
    statuses, asked = _block(["kept"] * 3)
    assert statuses == ["kept"] * 3
    assert [sides for _k, sides in asked] == [compare.order(k) for k in range(3)]


def test_a_discarded_pair_is_repeated_and_the_order_goes_on_alternating():
    """A repeat that restarted at B A would put two B A pairs side by side."""
    statuses, asked = _block(["kept", "discarded", "kept", "kept"])
    assert statuses == ["kept", "discarded", "kept", "kept"]
    assert [k for k, _sides in asked] == [0, 1, 2, 3]
    assert asked[3][1] == ("after", "before")


def test_repeats_stop_at_the_cap():
    """Without a cap, a machine that drifts all night repeats all night."""
    statuses, _asked = _block(["discarded"] * 10)
    assert statuses == ["discarded"] * 3


def test_the_cap_counts_the_block_not_the_run_of_discards():
    statuses, _asked = _block(["discarded", "kept", "discarded", "kept",
                               "discarded", "kept"])
    assert statuses == ["discarded", "kept", "discarded", "kept", "discarded"]


def test_a_pair_whose_run_died_is_repeated_like_a_discard():
    statuses, _asked = _block(["died", "kept", "kept", "kept"])
    assert statuses == ["died", "kept", "kept", "kept"]


def test_a_refusal_ends_the_block():
    """A refusal comes back every time, so a repeat only spends the cap."""
    statuses, _asked = _block(["refused", "kept", "kept", "kept"])
    assert statuses == ["refused"]


def test_no_repeats_with_a_cap_of_none():
    statuses, _asked = _block(["discarded", "kept"], pairs=1, repeats=0)
    assert statuses == ["discarded"]


# --------------------------------------------------------------------------
# The discard


def test_a_pair_is_discarded_only_past_the_threshold():
    assert compare.discarded([8.5, 8.6, 9.9], compare.THRESHOLD)
    assert not compare.discarded([8.5, 8.6, 9.7], compare.THRESHOLD)
    # "More than" the threshold: equal to it is kept.
    assert not compare.discarded([10.0, 11.5], 1.15)
    assert compare.THRESHOLD == 1.15


def test_the_threshold_stays_unless_the_first_pair_is_noisy_on_its_own():
    threshold, _how = compare.session_threshold([[8.51, 8.63, 8.55], [8.5, 8.6]])
    assert threshold == compare.THRESHOLD
    threshold, _how = compare.session_threshold([[10.0, 10.4]])
    assert threshold == compare.THRESHOLD


def test_a_noisy_first_pair_sets_the_threshold_at_three_times_its_spread():
    """At 1.15x, a machine whose calibration alone spreads 10% would discard
    every pair until the cap."""
    threshold, how = compare.session_threshold([[8.5, 8.6], [10.0, 11.0, 10.2]])
    assert threshold == pytest.approx(1.3)
    assert "10.0%" in how


def test_the_relaxed_threshold_stops_at_its_most():
    """One calibration caught in a drift (another job starting) would
    otherwise relax the threshold past any drift, and switch the rule off."""
    threshold, how = compare.session_threshold([[8.5, 8.6, 30.0], [8.5, 8.6]])
    assert threshold == compare.THRESHOLD_MOST == 1.5
    assert "too noisy" in how
    # Just under the most is kept as it is.
    threshold, _how = compare.session_threshold([[10.0, 11.6]])
    assert threshold == pytest.approx(1.48)


def test_a_single_run_calibration_says_nothing_of_its_own_spread():
    threshold, _how = compare.session_threshold([[10.0], [20.0]])
    assert threshold == compare.THRESHOLD


# --------------------------------------------------------------------------
# The noise floor


def test_neighbours_are_adjacent_runs_of_one_side():
    """ABBA puts the same side side by side at every boundary between pairs,
    and those runs are the measure of the noise."""
    runs = [("before", 10.0, True), ("after", 10.0, True),
            ("after", 11.0, True), ("before", 10.0, True),
            ("before", 9.0, True), ("after", 10.0, True)]
    assert compare.neighbours(runs) == pytest.approx([1.1, 0.9])


def test_a_run_that_is_not_usable_makes_no_neighbour():
    runs = [("before", 10.0, True), ("after", 10.0, True),
            ("after", 30.0, False), ("before", 10.0, True)]
    assert compare.neighbours(runs) == []


def test_the_floor_is_the_mean_distance_from_one():
    found = compare.floors({("p", 1): [1.1, 0.9, 1.05]})
    assert found[("p", 1)].f == pytest.approx(0.25 / 3)
    assert found[("p", 1)].count == 3


def test_few_neighbours_make_a_floor_of_at_least_two_percent():
    found = compare.floors({("p", 1): [1.01, 0.995]})
    assert found[("p", 1)].f == compare.FLOOR_LEAST == 0.02


def test_three_neighbours_keep_a_floor_under_two_percent():
    found = compare.floors({("p", 1): [1.01, 0.99, 1.01]})
    assert found[("p", 1)].f == pytest.approx(0.01)


def test_with_no_neighbours_the_floor_is_the_sessions_largest():
    found = compare.floors({("p", 1): [1.1, 0.9], ("q", 1): []})
    assert found[("q", 1)].f == pytest.approx(0.1)
    assert found[("q", 1)].count == 0


def test_with_no_neighbours_anywhere_the_floor_is_two_percent():
    found = compare.floors({("q", 4): []})
    assert found[("q", 4)].f == compare.FLOOR_LEAST


# --------------------------------------------------------------------------
# The ratios


REPORT = {
    "target": "pkg/mod.py",
    "kills": [{"kind": "CMP", "line": 3, "change": "Lt -> LtE", "code": 1,
               "killer": "tests/test_mod.py::test_a"},
              {"kind": "CMP", "line": 5, "change": "Gt -> GtE", "code": -1,
               "killer": "TIMEOUT"}],
    "survivors": [{"kind": "RAISE", "line": 7, "change": "raise -> pass"}],
    "accepted": [{"kind": "CONST", "line": 9, "change": "1 -> 2"}],
}


def _call(seconds, line=None, kind=None, change=None, prefix=""):
    return {"target": "pkg/mod.py", "n": None if line is None else 1, "kind": kind,
            "line": line, "change": change, "prefix": prefix, "seconds": seconds}


CALLS = [_call(10.0),  # the baseline
         _call(2.0, 3, "CMP", "Lt -> LtE"),
         _call(30.0, 5, "CMP", "Gt -> GtE"),
         _call(9.0, 7, "RAISE", "raise -> pass"),
         _call(8.0, 9, "CONST", "1 -> 2"),
         _call(0.5, prefix="probe-")]  # a gate: a run of the original on a file


def test_the_fixed_keys_are_the_before_sides_timeouts_and_survivors():
    assert compare.fixed_keys([REPORT], ["timeout"]) == {
        ("pkg/mod.py", "CMP", 5, "Gt -> GtE", None)}
    assert compare.fixed_keys([REPORT], ["survivor"]) == {
        ("pkg/mod.py", "RAISE", 7, "raise -> pass", None),
        ("pkg/mod.py", "CONST", 9, "1 -> 2", None)}
    assert compare.fixed_keys([REPORT], ["baseline"]) == set()


def test_the_fixed_seconds_are_the_baseline_and_the_named_mutants_runs():
    """A gate runs the original too, but on a file of its own, and it is
    what a feature like a probe adds: it stays in the touchable part."""
    keys = compare.fixed_keys([REPORT], ["timeout"])
    assert compare.fixed_seconds(CALLS, keys, ["baseline", "timeout"]) == 40.0
    assert compare.fixed_seconds(CALLS, keys, ["timeout"]) == 30.0
    every = compare.fixed_keys([REPORT], ["timeout", "survivor"])
    assert compare.fixed_seconds(CALLS, every, compare.FIXED_CLASSES) == 57.0


def test_the_ratios_are_before_over_after():
    """Each side less its own fixed seconds: a feature that adds a fixed
    cost (a coverage run) adds it on its side alone."""
    got = compare.ratios(300.0, 250.0, 130.0, 100.0)
    assert got.whole == pytest.approx(1.2)
    assert got.touchable == pytest.approx(170 / 150)
    assert got.ceiling == pytest.approx(300 / 130)


def test_without_the_instrument_there_is_no_touchable_part():
    got = compare.ratios(300.0, 250.0, None, 130.0)
    assert got == compare.Ratios(pytest.approx(1.2), None, None)


def test_a_touchable_part_that_is_not_above_zero_is_none():
    """A ratio with nothing, or less than nothing, on one side is no
    speedup: it is the instrument's seconds outrunning the wall clock."""
    got = compare.ratios(300.0, 100.0, 120.0, 100.0)
    assert got.touchable is None
    assert got.ceiling == pytest.approx(2.5)
    assert compare.ratios(100.0, 300.0, 120.0, 100.0).touchable is None


def test_the_touchable_floor_is_the_whole_runs_2f_in_the_touchable_part():
    """R40 at N = 1: W 305 s, U 60 s; at f = 2% a whole-run move of 2f is
    12.2 s, a fifth of U."""
    assert compare.touchable_floor(0.02, 305.0, 60.0) == pytest.approx(
        1 / (1 - 2 * 0.02 * 305 / 60))
    # 2f of the whole run is all of the touchable part: no speedup shows.
    assert compare.touchable_floor(0.1, 300.0, 60.0) is None
    assert compare.touchable_floor(0.1, 300.0, 59.0) is None
    assert compare.touchable_floor(0.02, None, 60.0) is None
    assert compare.touchable_floor(0.02, 305.0, None) is None


def test_a_touchable_speedup_is_judged_against_its_own_floor():
    """The whole run moving by more than 2f is the test, seen through the
    touchable part, in either direction."""
    needs = compare.touchable_floor(0.02, 305.0, 60.0)
    assert compare.touchable_beyond(needs * 1.01, 0.02, 305.0, 60.0) is True
    assert compare.touchable_beyond(needs * 0.99, 0.02, 305.0, 60.0) is False
    slow = 1 / (1 + 2 * 0.02 * 305 / 60)
    assert compare.touchable_beyond(slow * 0.99, 0.02, 305.0, 60.0) is True
    assert compare.touchable_beyond(slow * 1.01, 0.02, 305.0, 60.0) is False
    assert compare.touchable_beyond(3.0, 0.1, 300.0, 60.0) is False
    assert compare.touchable_beyond(None, 0.02, 305.0, 60.0) is None


def test_a_speedup_beyond_the_floor_is_more_than_2f_from_one():
    """Inside `2f`, two runs of one side differ as much: it is no change."""
    floor = compare.Floor(0.012, 4, "measured")
    assert _row(whole=1.02, floor=floor).beyond_floor is False
    assert _row(whole=0.98, floor=floor).beyond_floor is False
    assert _row(whole=1.03, floor=floor).beyond_floor is True
    assert _row(whole=0.97, floor=floor).beyond_floor is True
    assert _row(whole=None).beyond_floor is None


# --------------------------------------------------------------------------
# Verdicts


def _with(**changes):
    report = {key: [dict(e) for e in value] if isinstance(value, list) else value
              for key, value in REPORT.items()}
    report.update(changes)
    return report


def test_equal_verdicts_are_no_difference():
    first = compare.verdicts([REPORT])
    assert not compare.compare_verdicts(first, compare.verdicts([REPORT]))


def test_a_different_killer_is_the_same_kill():
    """Which test fails first can change with the order of the run, and a
    kill is a kill."""
    other = _with(kills=[dict(REPORT["kills"][0], killer="tests/test_mod.py::test_b"),
                         REPORT["kills"][1]])
    assert not compare.compare_verdicts(compare.verdicts([REPORT]),
                                        compare.verdicts([other]))


def test_a_kill_that_survives_on_the_other_side_is_a_real_difference():
    other = _with(kills=REPORT["kills"][1:],
                  survivors=REPORT["survivors"] + [
                      {"kind": "CMP", "line": 3, "change": "Lt -> LtE"}])
    diff = compare.compare_verdicts(compare.verdicts([REPORT]),
                                    compare.verdicts([other]))
    assert diff and [key for key, _a, _b in diff.real] == [
        ("pkg/mod.py", "CMP", 3, "Lt -> LtE", None)]
    assert diff.load == []


def test_a_kill_by_time_that_survives_elsewhere_is_put_down_to_load():
    other = _with(kills=REPORT["kills"][:1],
                  survivors=REPORT["survivors"] + [
                      {"kind": "CMP", "line": 5, "change": "Gt -> GtE"}])
    diff = compare.compare_verdicts(compare.verdicts([REPORT]),
                                    compare.verdicts([other]))
    assert diff and diff.real == [] and len(diff.load) == 1


def test_a_kill_a_signal_ended_is_put_down_to_load():
    killed = _with(kills=[{"kind": "RAISE", "line": 7, "change": "raise -> pass",
                           "code": -9, "killer": ""}], survivors=[])
    diff = compare.compare_verdicts(compare.verdicts([REPORT]),
                                    compare.verdicts([killed]))
    assert ("pkg/mod.py", "RAISE", 7, "raise -> pass", None) in [k for k, _a, _b in diff.load]


def test_a_mutant_only_one_side_has_is_a_real_difference():
    other = _with(accepted=[])
    diff = compare.compare_verdicts(compare.verdicts([REPORT]),
                                    compare.verdicts([other]))
    assert [key for key, _a, _b in diff.real] == [("pkg/mod.py", "CONST", 9, "1 -> 2", None)]


def test_accepted_and_survived_are_different_verdicts():
    other = _with(accepted=[], survivors=REPORT["survivors"] + REPORT["accepted"])
    assert compare.compare_verdicts(compare.verdicts([REPORT]),
                                    compare.verdicts([other])).real


def test_the_plugins_targets_are_kept_apart():
    """The plugin reports a list, one per target, and the same line and
    change in two modules are two mutants."""
    two = [REPORT, dict(REPORT, target="pkg/other.py")]
    assert len(compare.verdicts(two)) == 2 * len(compare.verdicts([REPORT]))


#: One line with the same edit twice: `recipes.py` has such lines (two
#: `and`s at line 899), so kind, line and change name two mutants.
TWIN_SOURCE = "def both(a, b, c, d):\n    return (a and b, c and d)\n"
TWINS = [TWIN_SOURCE.replace("a and b", "a or b"),
         TWIN_SOURCE.replace("c and d", "c or d")]


def _twin(text):
    """A report entry for one of the twins, its diff made as the engine
    makes it."""
    diff = "\n".join(difflib.unified_diff(
        TWIN_SOURCE.splitlines(), text.splitlines(), fromfile="dup.py",
        tofile="dup.py (mutant)", lineterm=""))
    return {"kind": "BOOL", "line": 2, "change": "And -> Or", "diff": diff}


def _twins(killed, survived):
    return [{"target": "dup.py", "accepted": [],
             "kills": [dict(_twin(TWINS[i]), code=1, killer="t.py::test")
                       for i in killed],
             "survivors": [_twin(TWINS[i]) for i in survived]}]


def test_twins_on_one_line_are_two_mutants():
    assert len(compare.verdicts(_twins([0], [1]))) == 2
    assert not compare.compare_verdicts(compare.verdicts(_twins([0], [1])),
                                        compare.verdicts(_twins([0], [1])))


def test_a_twins_lost_kill_is_a_real_difference():
    """Keyed on kind, line and change, the survivor overwrote the kill on
    both sides, and a kill lost after read as no difference at all."""
    diff = compare.compare_verdicts(compare.verdicts(_twins([0], [1])),
                                    compare.verdicts(_twins([], [0, 1])))
    assert [(a[0], b[0]) for _key, a, b in diff.real] == [("killed", "survived")]


def test_twins_that_swap_their_verdicts_are_two_differences():
    diff = compare.compare_verdicts(compare.verdicts(_twins([0], [1])),
                                    compare.verdicts(_twins([1], [0])))
    assert len(diff.real) == 2


def test_twins_without_a_diff_are_still_counted_apart():
    """An engine that writes no diff leaves no mark, and the count is
    what keeps one twin from hiding the other."""
    bare = _twins([0], [1])
    for entry in bare[0]["kills"] + bare[0]["survivors"]:
        del entry["diff"]
    assert sorted(v[0] for v in compare.verdicts(bare).values()) == [
        "killed", "survived"]


def test_a_twins_runs_are_its_own_in_the_touchable_part():
    """The twin killed by time is fixed; its sibling, killed by a test,
    stays touchable."""
    report = _twins([0], [])
    report[0]["kills"].append(dict(_twin(TWINS[1]), code=-1, killer="TIMEOUT"))
    keys = compare.fixed_keys(report, ["timeout"])
    rows = [{"target": "dup.py", "n": i + 1, "kind": "BOOL", "line": 2,
             "change": "And -> Or", "prefix": "", "seconds": seconds,
             "mark": compare.mutant_mark(_twin(TWINS[i])["diff"])}
            for i, seconds in enumerate([2.0, 30.0])]
    assert compare.fixed_seconds(rows, keys, ["timeout"]) == 30.0


def test_the_instruments_mark_is_the_reports():
    instrument = importlib.util.spec_from_file_location(
        "bench_instrument", os.path.join(BENCH, "instrument.py"))
    module = importlib.util.module_from_spec(instrument)
    instrument.loader.exec_module(module)
    marks = [module.mark(TWIN_SOURCE, text) for text in TWINS]
    assert marks == [compare.mutant_mark(_twin(text)["diff"]) for text in TWINS]
    assert marks[0] != marks[1]


def test_a_real_campaigns_twins_and_its_instrument_rows_agree(tmp_path):
    """invective itself, through the instrument, on a line holding the same
    edit twice: the report has both twins with their own verdicts, and each
    instrument row's mark is one of the report's."""
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n",
                                             encoding="utf-8")
    # CRLF, so that the instrument has to split its lines as the report
    # does for its marks to be the report's.
    (tmp_path / "dup.py").write_bytes(TWIN_SOURCE.replace("\n", "\r\n").encode())
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_dup.py").write_text(
        "from dup import both\n\n"
        "def test_first():\n"
        "    assert both(True, False, 0, 0)[0] is False\n", encoding="utf-8")
    calls, marks, report = (str(tmp_path / name) for name in
                            ("calls.jsonl", "marks.json", "report.json"))
    done = subprocess.run(
        [sys.executable, os.path.join(BENCH, "instrument.py"), "--calls", calls,
         "--marks", marks, "run", "--target", "dup.py", "--tests",
         "tests/test_dup.py", "--only", "BOOL", "--json", report],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    assert done.returncode == 0, done.stdout + done.stderr
    with open(report, encoding="utf-8") as fh:
        said = compare.verdicts([json.load(fh)])
    assert sorted(v[0] for v in said.values()) == ["killed", "survived"]
    with open(calls, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    assert sorted(row["mark"] for row in rows if row["n"] is not None) == sorted(
        key[-1] for key in said)


# --------------------------------------------------------------------------
# The Finding


def _info(**changes):
    info = {
        "date": "2026-10-10", "cpu": "AMD Ryzen 7 7840U",
        "cores": "8 cores, 16 logical CPUs", "os": "Linux-6.11", "python": "Python 3.14.0",
        "before": {"ref": "main", "sha": "f989e8f" + "0" * 33, "settings": [], "env": []},
        "after": {"ref": "topic", "sha": "abcdef1" + "0" * 33,
                  "settings": [("coverage", "true")], "env": []},
        "cases": {"R40": ("invective run --target more_itertools/recipes.py --tests "
                          "tests/test_recipes.py --only CMP,BOOL,NOT,CONST,RAISE "
                          "--limit 40", "more-itertools", "more-itertools at `81c21a8`")},
        "deselected": {"more-itertools": (
            "tests/test_more.py::TestConcurrentTee::test_concurrent_consumers",)},
        "pairs": 3, "workers": [1, 4],
        "calibration": {"more-itertools": "`pytest -q tests/test_recipes.py`, the "
                                          "median of 3"},
        "threshold": "1.15x", "fixed": "baseline, timeout"}
    info.update(changes)
    return info


def _row(**changes):
    row = dict(case="R40", project="more-itertools", workers=4, asked=3, kept=3,
               discarded=0, died=0, before=305.0, after=100.0, whole=3.05,
               whole_low=3.0, whole_high=3.1, touchable=None, ceiling=None,
               floor=compare.Floor(0.012, 4, "measured"), verdicts="equal")
    row.update(changes)
    return compare.Row(**row)


def _joined(text):
    """The Finding's prose as one line, as markdown reads a wrapped one."""
    return " ".join(line.removeprefix("> ") for line in text.splitlines())


def test_the_finding_opens_in_the_form_CLAUDE_md_gives():
    text = compare.finding(_info(), [_row()])
    assert re.match(r"> \*\*Finding:\*\* \(2026-10-10, ", text)
    assert text.splitlines()[-1].startswith("> ")


def test_the_finding_names_its_conditions():
    """A Finding without its machine, its commits or its cases cannot be
    taken again, or set beside another."""
    joined = _joined(compare.finding(_info(), [_row()]))
    for said in ("AMD Ryzen 7 7840U", "8 cores, 16 logical CPUs", "Linux-6.11",
                 "Python 3.14.0", "`main` at `f989e8f`", "`topic` at `abcdef1`",
                 "`coverage = true`", "--limit 40", "more-itertools at `81c21a8`",
                 "`tests/test_more.py::TestConcurrentTee::test_concurrent_consumers`",
                 "`PYTEST_ADDOPTS`", "workers 1, 4", "3 ABBA pair(s)",
                 "`pytest -q tests/test_recipes.py`"):
        assert said in joined, said


def test_the_finding_wraps_at_90_and_holds_no_dash_aside():
    """It is pasted into docs/, where `tests/test_docs.py` holds every line
    to 90 characters and takes ` - ` for an aside."""
    text = compare.finding(_info(), [_row(), _row(workers=1, touchable=1.4,
                                                  ceiling=1.25, touchable_part=60.0)])
    for line in text.splitlines():
        assert len(line) <= 90, line
        # A table row in a quote is prose to `test_docs`, which strips only a
        # line's leading `|`.
        assert not re.search(r" --? |–|—", re.sub(r"`[^`]*`", "``", line)), line


def test_the_finding_states_the_numbers_and_the_verdicts():
    text = compare.finding(_info(), [_row()])
    assert "| R40 | 4 | 3 of 3 | 305.0 s | 100.0 s | 3.05x | none | 1.2% |" in text
    joined = _joined(text)
    assert "R40 at N = 4 runs 3.05x faster after than before" in joined
    assert "same kills, survivors and acceptances" in joined


def test_the_finding_gives_W_U_and_what_the_touchable_floor_needs():
    row = _row(workers=1, before=305.0, touchable=1.4, ceiling=1.25,
               touchable_part=60.0, floor=compare.Floor(0.02, 4, "measured"))
    text = compare.finding(_info(), [row])
    assert "> | R40 | 1 | 305.0 s | 60.0 s | 1.40x | 1.26x |" in text
    assert ("The touchable part's floor needs 1.26x (`1 / (1 - 2f W / U)`, W 305.0 s, "
            "U 60.0 s), so the touchable speedup is beyond it.") in _joined(text)
    inside = _joined(compare.finding(_info(), [dataclasses.replace(
        row, touchable=1.2)]))
    assert "so the touchable speedup is inside it" in inside
    none = _joined(compare.finding(_info(), [dataclasses.replace(
        row, floor=compare.Floor(0.2, 4, "measured"))]))
    assert "no speedup of the touchable part shows beyond the floor" in none
    head, line = (re.split(r"\s{2,}", got) for got in compare.table([row]))
    said = dict(zip(head, line))
    assert (said["U"], said["touch. needs"], said["touch. beyond"]) == (
        "60.0 s", "1.26x", "yes")


def test_the_finding_says_a_slowdown_is_one():
    joined = _joined(compare.finding(_info(), [_row(whole=0.8, whole_low=0.79,
                                                    whole_high=0.81)]))
    assert "1.25x slower after than before" in joined


def test_the_finding_says_a_ratio_that_rounds_to_one_is_as_fast():
    """A ratio of 0.999 said as 1.00x slower reads as a slowdown the numbers
    do not show."""
    joined = _joined(compare.finding(_info(), [_row(whole=0.999, whole_low=0.999,
                                                    whole_high=0.999)]))
    assert "R40 at N = 4 runs as fast after as before" in joined


def test_the_finding_says_a_ratio_inside_the_floor_is_no_change():
    joined = _joined(compare.finding(_info(), [_row(whole=1.01)]))
    assert "inside 2f" in joined


def test_the_finding_says_the_verdicts_differ():
    joined = _joined(compare.finding(_info(), [_row(verdicts="DIFFER")]))
    assert "verdicts differ between runs of R40" in joined
    assert "same kills" not in joined


def test_the_finding_says_a_difference_put_down_to_load_is_one():
    joined = _joined(compare.finding(_info(), [_row(verdicts="load only")]))
    assert "differ only by kills by time or by a signal" in joined
    assert "same kills" not in joined


def test_the_finding_says_a_case_short_of_its_pairs_is_inconclusive():
    joined = _joined(compare.finding(_info(), [_row(kept=2, discarded=3)]))
    assert "inconclusive" in joined


# --------------------------------------------------------------------------
# The cases and the settings


def test_the_cases_are_the_measurement_harnesss():
    """The cases are what every speedup's Finding names, and one that
    drifted would no longer be the case other Findings measured."""
    projects, cases = compare.load_cases(compare.CASES)
    assert cases["R40"].command() == (
        "invective run --target more_itertools/recipes.py --tests "
        "tests/test_recipes.py --only CMP,BOOL,NOT,CONST,RAISE --limit 40")
    assert cases["M60"].command() == (
        "invective run --target more_itertools/more.py --tests tests/test_more.py "
        "--only CMP,RAISE,BOOL --limit 60")
    assert cases["RALL"].limit is None
    assert cases["RALL"].command() == cases["R40"].command().removesuffix(" --limit 40")
    assert cases["I20"].command() == (
        "pytest -n 0 -p no:cacheprovider --mutate src/invective/accept.py "
        "--mutate-only CMP,BOOL,NOT,CONST,RAISE tests/test_accept.py")
    assert (cases["T120"].target, cases["T120"].limit) == ("src/attr/_make.py", 120)
    mit = projects["more-itertools"]
    assert mit.commit == "81c21a8db1c922260cd673841257fb9b112c1318"
    assert mit.deselect == (
        "tests/test_more.py::TestConcurrentTee::test_concurrent_consumers",)
    assert projects["attrs"].commit == "644b4e165bfbeee7e127de6fcbda08b64014316f"


def test_a_misspelt_key_in_the_cases_is_refused(tmp_path):
    path = tmp_path / "cases.toml"
    path.write_text('[projects.p]\npath = "p"\n[cases.C]\nproject = "p"\n'
                    'target = "m.py"\ntests = ["t.py"]\nlimt = 3\n', encoding="utf-8")
    with pytest.raises(compare.BenchError, match="limt"):
        compare.load_cases(str(path))


def test_a_short_commit_is_refused(tmp_path):
    """`git fetch` takes only a full sha from a server that has not
    advertised it."""
    path = tmp_path / "cases.toml"
    path.write_text('[projects.p]\nrepo = "https://example.invalid/p"\n'
                    'commit = "81c21a8"\n', encoding="utf-8")
    with pytest.raises(compare.BenchError, match="full sha"):
        compare.load_cases(str(path))


def test_a_setting_goes_into_its_table():
    text = compare.insert_into_table(
        "[project]\nname = 'x'\n", "tool.invective",
        ["coverage = %s" % compare.toml_value("true"),
         "workers = %s" % compare.toml_value("auto")])
    assert text.endswith('[tool.invective]\ncoverage = true\nworkers = "auto"\n')


def test_a_setting_goes_into_the_table_that_is_there():
    text = compare.insert_into_table(
        "[tool.pytest]\nstrict = true\n\n[tool.other]\nx = 1\n", "tool.pytest",
        ['pythonpath = ["src"]'])
    assert text.startswith('[tool.pytest]\npythonpath = ["src"]\nstrict = true\n')


def test_a_setting_the_project_already_has_is_refused():
    """The last of two would win silently in a reader that allowed it."""
    with pytest.raises(compare.BenchError):
        compare.insert_into_table("[tool.invective]\nconfirm = true\n",
                                  "tool.invective", ["confirm = false"])


# --------------------------------------------------------------------------
# A run's status, and the command line's refusals


def _timed(code, timed_out=False):
    return compare.Timed(code, 1.0, None, None, timed_out)


def test_a_run_is_ok_only_when_it_ran_and_reported():
    """A run that crashed after writing its report is not a measurement of
    the campaign, and a run with no report has no verdicts to compare."""
    assert compare.status_of(_timed(0), True) == "ok"
    assert compare.status_of(_timed(1), True) == "ok"
    assert compare.status_of(_timed(0), False) == "died"
    assert compare.status_of(_timed(3), True) == "died"
    assert compare.status_of(_timed(-9), True) == "died"
    assert compare.status_of(_timed(2), False) == "refused"
    assert compare.status_of(_timed(-15, timed_out=True), True) == "timeout"


def test_an_out_directory_with_something_in_it_is_refused(tmp_path, capsys):
    """Rows appended to another session's files would be read as its own."""
    (tmp_path / "runs.tsv").write_text("seq\n", encoding="utf-8")
    assert compare.main(["HEAD", "HEAD", "--repo", ROOT, "--out", str(tmp_path)]) == 2
    assert "not empty" in capsys.readouterr().err
    assert os.listdir(tmp_path) == ["runs.tsv"]


# --------------------------------------------------------------------------
# The environment and the deselection


def _session(tmp_path, project, **sides):
    """A session whose before side is this interpreter, on *project* in
    *tmp_path*, with nothing set up: for one step of a session alone."""
    args = argparse.Namespace(fixed="baseline,timeout", out=str(tmp_path / "out"),
                              repo=ROOT, threshold=None)
    # The sides' copies are not where the tool's own pytests run.
    session = compare.Session(args, {project.name: project}, [], [1], 1, 0, {
        label: compare.Side(label, "HEAD", python=sys.executable,
                            dirs={project.name: str(tmp_path / "no-such-side")}, **sides)
        for label in compare.SIDES})
    session.calibration_dirs = {project.name: str(tmp_path)}
    return session


def test_a_runs_environment_carries_the_deselections_then_the_sides_options(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-ra")
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    project = compare.Project("p", path=str(tmp_path), deselect=("t.py::a", "t.py::b"))
    session = _session(tmp_path, project, env=[("PYTEST_ADDOPTS", "-x"), ("K", "v")])
    env = session.env_for(session.sides["before"], project)
    assert env["PYTEST_ADDOPTS"] == "-ra --deselect t.py::a --deselect t.py::b -x"
    assert env["K"] == "v"
    # An outside PYTHONPATH would put another copy of a package ahead of
    # the venv's, on one side and not the other.
    assert "PYTHONPATH" not in env
    assert env["PATH"].startswith(os.path.dirname(sys.executable) + os.pathsep)


def _deselecting(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n",
                                             encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_kept():\n    pass\n\ndef test_dropped():\n    pass\n",
        encoding="utf-8")
    return compare.Project("p", path=str(tmp_path),
                           deselect=("tests/test_x.py::test_dropped",))


def test_a_deselection_is_checked_against_a_collection(tmp_path):
    project = _deselecting(tmp_path)
    session = _session(tmp_path, project)
    session._check_deselected(project, str(tmp_path / "log"))


def test_a_cases_collected_tests_are_the_files_named_less_the_excluded(tmp_path):
    """T120's tests: every file a collection names, in the third copy, as
    plain paths, less the excluded."""
    project = _deselecting(tmp_path)
    (tmp_path / "tests" / "test_skipme.py").write_text("def test_y():\n    pass\n",
                                                       encoding="utf-8")
    session = _session(tmp_path, project)
    case = compare.Case("T", "p", "m.py", collect=("tests",),
                        collect_exclude=("skipme",))
    session._collect(case, str(tmp_path / "log"))
    assert session.tests[("T", "before")] == ("tests/test_x.py",)


def test_a_deselection_that_does_not_take_stops_the_session(tmp_path, monkeypatch):
    """A deselection that does not reach pytest leaves the test that times
    the machine in every run, and nothing in the numbers says so."""
    project = _deselecting(tmp_path)
    session = _session(tmp_path, project)
    monkeypatch.setattr(session, "env_for", lambda side, p: dict(os.environ))
    with pytest.raises(compare.BenchError, match="still collects"):
        session._check_deselected(project, str(tmp_path / "log"))


# --------------------------------------------------------------------------
# Units, the pytest processes and the confirm run, against a stand-in engine


def _report(timeouts=1, unreproduced=None, survived=()):
    kills = [{"kind": "CMP", "line": 1, "change": "Lt -> LtE", "code": 1,
              "killer": "t.py::test_a"}]
    kills += [{"kind": "CMP", "line": 2 + i, "change": "Gt -> GtE", "code": -1,
               "killer": "TIMEOUT"} for i in range(timeouts)]
    report = {"target": "m.py", "kills": kills, "accepted": [],
              "survivors": [{"kind": "RAISE", "line": 9, "change": "raise -> pass"}
                            for _ in survived]}
    if unreproduced is not None:
        report["unreproduced"] = unreproduced
    return report


def _calls(mutants=2, baselines=1):
    rows = [{"n": None, "prefix": "", "seconds": 0.5, "target": "m.py", "kind": None,
             "line": None, "change": None}] * baselines
    return rows + [{"n": i + 1, "prefix": "", "seconds": 0.2, "target": "m.py",
                    "kind": "CMP", "line": i + 1, "change": "x"} for i in range(mutants)]


class _Engine:
    """`run_timed` in place of a run of invective: it says whether the
    run's project held a `.invective` when it started, leaves one behind, as
    an engine that remembers does, and writes what *behave* gives it for
    the run (a report, instrument rows and an exit code)."""

    def __init__(self, behave, calibrations=()):
        self.behave = behave
        self.seen = []
        self.argvs = []
        self.cwds = []
        #: The seconds each calibration run takes, in turn; and where each
        #: ran. A run of plain pytest, with no instrument, is one.
        self.calibrations = list(calibrations)
        self.calibrated_in = []

    def __call__(self, argv, cwd, env, log, timeout=None):
        if compare.INSTRUMENT not in argv:
            self.calibrated_in.append(cwd)
            return compare.Timed(0, self.calibrations.pop(0), None, None, False)
        self.cwds.append(cwd)
        memory = os.path.join(cwd, ".invective")
        self.seen.append(os.path.exists(memory))
        os.makedirs(memory, exist_ok=True)
        with open(os.path.join(memory, "remembered"), "w") as fh:
            fh.write("x")
        self.argvs.append(argv)
        side = "before" if os.path.basename(cwd) == "before" else "after"
        prime = "-prime" in log
        report, calls, code = self.behave(side, prime, argv)

        def after(flag):
            return argv[argv.index(flag) + 1]

        out = after("--json") if "--json" in argv else after("--mutate-json")
        with open(out, "w") as fh:
            json.dump(report, fh)
        with open(after("--calls"), "w") as fh:
            fh.writelines(json.dumps(row) + "\n" for row in calls)
        with open(after("--marks"), "w") as fh:
            fh.write("{}")
        with open(log, "a") as fh:
            fh.write("ran\n")
        return compare.Timed(code, 5.0 if prime else 1.0, None, None, False)


def _engine_session(tmp_path, monkeypatch, behave, pairs=1, via="run",
                    calibrations=(), repeats=0, **flags):
    """A session of one case on a project of nothing, its runs made by
    `_Engine`: the schedule, the units and the checks, without a venv.
    With *calibrations*, the project is calibrated, one run a calibration,
    each taking the next of them in seconds."""
    engine = _Engine(behave, calibrations)
    monkeypatch.setattr(compare, "run_timed", engine)
    options = dict(fixed="baseline,timeout", out=str(tmp_path / "out"), repo=ROOT,
                   threshold=None, timeout=None, unit="cold", same_processes=False,
                   expect_rerun_processes=False, confirm_run=False)
    options.update(flags)
    args = argparse.Namespace(**options)
    sides = {}
    for label in compare.SIDES:
        (tmp_path / label).mkdir()
        sides[label] = compare.Side(label, "HEAD", sha="0" * 40, python=sys.executable,
                                    dirs={"p": str(tmp_path / label)})
    case = compare.Case("C", "p", "m.py", ("t.py",), via=via)
    project = compare.Project("p", path="x", calibrate=("t.py",) if calibrations else (),
                              calibrate_runs=1)
    session = compare.Session(args, {"p": project}, [case], [1], pairs, repeats, sides)
    (tmp_path / "calibration").mkdir()
    session.calibration_dirs = {"p": str(tmp_path / "calibration")}
    os.makedirs(session.logs)
    session.tests = {("C", label): ("t.py",) for label in compare.SIDES}
    return session, engine, case


def _same(side, prime, argv):
    return _report(), _calls(), 0


def test_a_cold_unit_runs_each_slot_once_from_nothing_remembered(tmp_path, monkeypatch):
    """A run that found the last one's memory would be warm, and the one
    after it in the pair would be timed against a different engine."""
    session, engine, _case = _engine_session(tmp_path, monkeypatch, _same, pairs=2)
    session.run()
    assert engine.seen == [False] * 4
    runs = _rows(tmp_path / "out" / "runs.tsv")
    assert [(r["unit"], r["prime_wall"]) for r in runs] == [("cold", "")] * 4


def test_a_warm_unit_times_the_run_that_remembers_the_untimed_one(tmp_path, monkeypatch):
    """Each slot starts from nothing, so neither side and no pair inherits
    another's memory; the second run in it is the one that remembers."""
    session, engine, _case = _engine_session(tmp_path, monkeypatch, _same, pairs=2,
                                             unit="warm")
    session.run()
    assert engine.seen == [False, True] * 4
    runs = _rows(tmp_path / "out" / "runs.tsv")
    assert len(runs) == 4
    assert {(r["unit"], r["wall"], r["prime_wall"], r["prime_status"]) for r in runs} == {
        ("warm", "1.000", "5.000", "ok")}
    assert {(r["prime_killed"], r["prime_timeouts"], r["prime_runs"]) for r in runs} == {
        ("2", "1", "3")}
    assert [p["speedup"] for p in _rows(tmp_path / "out" / "pairs.tsv")] == ["1.000"] * 2


def test_an_untimed_run_that_differs_is_a_verdict_difference(tmp_path, monkeypatch):
    def behave(side, prime, argv):
        return _report(survived=[1] if prime and side == "after" else []), _calls(), 0

    session, _engine, _case = _engine_session(tmp_path, monkeypatch, behave,
                                              unit="warm")
    session.run()
    assert session.differ["C"] == "DIFFER"
    assert any("untimed" in line for line in session.differences)


def test_an_untimed_run_that_dies_ends_its_slot(tmp_path, monkeypatch):
    def behave(side, prime, argv):
        return _report(), _calls(), 3 if prime else 0

    session, engine, _case = _engine_session(tmp_path, monkeypatch, behave,
                                             unit="warm")
    session.run()
    runs = _rows(tmp_path / "out" / "runs.tsv")
    assert [(r["status"], r["prime_status"], r["wall"]) for r in runs][0] == (
        "died", "died", "")
    assert [p["status"] for p in _rows(tmp_path / "out" / "pairs.tsv")] == ["died"]


def _processes(after_rows):
    def behave(side, prime, argv):
        return _report(), _calls(after_rows if side == "after" else 2), 0
    return behave


def test_the_before_sides_touchable_seconds_are_the_pairs_U(tmp_path, monkeypatch):
    """U is the before run's wall less its baseline and kills by time: one
    second, less the 0.5 s baseline (the stand-in's mutants are not the
    report's kill by time)."""
    session, _engine, _case = _engine_session(tmp_path, monkeypatch, _same)
    session.run()
    pair, = _rows(tmp_path / "out" / "pairs.tsv")
    assert float(pair["before_touchable"]) == pytest.approx(0.5)
    assert session.rows()[0].touchable_part == pytest.approx(0.5)


def test_the_tools_own_pytests_run_in_a_third_copy(tmp_path, monkeypatch):
    """What a calibration leaves in its tree (a `.hypothesis` database) would
    be copied into one side's campaigns and not the other's."""
    session, engine, _case = _engine_session(tmp_path, monkeypatch, _same, pairs=2,
                                             calibrations=[8.0] * 20)
    session.run()
    assert set(engine.calibrated_in) == {str(tmp_path / "calibration")}
    assert set(engine.cwds) == {str(tmp_path / "before"), str(tmp_path / "after")}


def test_the_warm_up_runs_in_the_third_copy(tmp_path, monkeypatch):
    session, engine, _case = _engine_session(tmp_path, monkeypatch, _same,
                                             calibrations=[8.0])
    session._warm(str(tmp_path / "log"))
    assert engine.calibrated_in == [str(tmp_path / "calibration")]


def test_a_project_is_placed_three_times(tmp_path):
    """One copy for each side, with its settings, and one for the tool's
    own pytests, with the project's edits and no side's settings."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[tool.pytest.ini_options]\n",
                                           encoding="utf-8")
    project = compare.Project("p", path=str(source),
                              pyproject=(("tool.pytest.ini_options", ("x = 1",)),))
    session = _session(tmp_path / "s", project)
    session.work = str(tmp_path / "work")
    session.sides = {label: compare.Side(label, "HEAD", settings=[("confirm", "true")])
                     for label in compare.SIDES}
    session._place(project, str(tmp_path / "log"))
    where = {label: session.sides[label].dirs["p"] for label in compare.SIDES}
    where["calibration"] = session.calibration_dirs["p"]
    assert len(set(where.values())) == 3
    for label, path in where.items():
        with open(os.path.join(path, "pyproject.toml"), encoding="utf-8") as fh:
            said = tomllib.loads(fh.read())
        assert said["tool"]["pytest"]["ini_options"] == {"x": 1}
        assert ("invective" in said["tool"]) == (label != "calibration"), label


def test_the_pairs_process_counts_are_recorded(tmp_path, monkeypatch):
    session, _engine, _case = _engine_session(tmp_path, monkeypatch, _processes(3))
    session.run()
    pair, = _rows(tmp_path / "out" / "pairs.tsv")
    assert (pair["before_processes"], pair["after_processes"],
            pair["same_processes"]) == ("3", "4", "False")
    assert session.process_failures == []
    row, = session.rows()
    assert row.processes == "3/4 differ"


def test_same_processes_fails_on_a_pair_whose_counts_differ(tmp_path, monkeypatch):
    """A cold run with nothing remembered must start what the engine before
    it started; a different count is a different engine, whatever the time."""
    session, _engine, _case = _engine_session(tmp_path, monkeypatch, _processes(3),
                                              same_processes=True)
    session.run()
    assert session.process_failures == [
        "C N=1 pair 1: before started 3 pytest processes, after 4"]


def test_same_processes_passes_on_equal_counts(tmp_path, monkeypatch):
    session, _engine, _case = _engine_session(tmp_path, monkeypatch, _processes(2),
                                              same_processes=True)
    session.run()
    assert session.process_failures == []
    assert session.rows()[0].processes == "3/3"


def test_same_processes_fails_without_the_instrument(tmp_path, monkeypatch):
    """No count is no evidence the counts agree."""
    def behave(side, prime, argv):
        return _report(), [] if side == "after" else _calls(), 0

    session, _engine, _case = _engine_session(tmp_path, monkeypatch, behave,
                                              same_processes=True)
    session.run()
    assert len(session.process_failures) == 1
    pair, = _rows(tmp_path / "out" / "pairs.tsv")
    assert (pair["before_processes"], pair["after_processes"]) == ("3", "")
    assert session.rows()[0].processes == "-"


def test_a_rerun_starts_its_baselines_and_one_run_a_kill_by_time():
    calls = _calls(mutants=5, baselines=4) + [
        {"n": None, "prefix": "probe-", "seconds": 0.1}]
    assert compare.rerun_expected(calls, [_report(timeouts=2)]) == 6
    assert compare.rerun_expected(None, [_report()]) is None


def _rerun(rows):
    def behave(side, prime, argv):
        if side == "after" and not prime:
            return _report(), _calls(mutants=rows - 1), 0
        return _report(), _calls(), 0
    return behave


def test_expect_rerun_processes_holds_the_after_timed_run_to_its_count(
        tmp_path, monkeypatch):
    """The untimed run kills one mutant by time and has one baseline, so a
    re-run that remembers the rest starts two."""
    session, _engine, _case = _engine_session(
        tmp_path, monkeypatch, _rerun(2), unit="warm", expect_rerun_processes=True)
    session.run()
    assert session.process_failures == []
    pair, = _rows(tmp_path / "out" / "pairs.tsv")
    assert pair["expected_processes"] == "2"
    assert _rows(tmp_path / "out" / "runs.tsv")[1]["expected_runs"] == "2"
    assert session.rows()[0].rerun == "ok"


def test_expect_rerun_processes_fails_a_rerun_that_runs_more(tmp_path, monkeypatch):
    session, _engine, _case = _engine_session(
        tmp_path, monkeypatch, _rerun(3), unit="warm", expect_rerun_processes=True)
    session.run()
    assert session.process_failures == [
        "C N=1 pair 1: the timed run after started 3 pytest processes, not the 2 "
        "expected of a re-run"]
    assert session.rows()[0].rerun == "missed"


def test_expect_rerun_processes_fails_without_the_instrument(tmp_path, monkeypatch):
    def behave(side, prime, argv):
        return _report(), [] if prime else _calls(), 0

    session, _engine, _case = _engine_session(
        tmp_path, monkeypatch, behave, unit="warm", expect_rerun_processes=True)
    session.run()
    assert session.process_failures == [
        "C N=1 pair 1: the timed run after started 3 pytest processes, not the None "
        "expected of a re-run"]


def test_one_missed_rerun_marks_the_case_missed(tmp_path, monkeypatch):
    timed = []

    def behave(side, prime, argv):
        if side == "after" and not prime:
            timed.append(1)
            return _report(), _calls(mutants=len(timed)), 0
        return _report(), _calls(), 0

    session, _engine, _case = _engine_session(
        tmp_path, monkeypatch, behave, pairs=2, unit="warm",
        expect_rerun_processes=True)
    session.run()
    assert len(session.process_failures) == 1
    assert session.rows()[0].rerun == "missed"


def test_unreproduced_is_read_from_every_report():
    assert compare.unreproduced_of([_report(unreproduced=[])]) == []
    assert compare.unreproduced_of([_report(unreproduced=[{"line": 1}]),
                                    _report(unreproduced=[{"line": 2}])]) == [
        {"line": 1}, {"line": 2}]
    # Without the key, the kills were not confirmed: no answer, not none.
    assert compare.unreproduced_of([_report(unreproduced=[]), _report()]) is None
    assert compare.unreproduced_of([]) is None


@pytest.mark.parametrize("via, flag", [("run", "--confirm"),
                                       ("pytest", "--mutate-confirm")])
@pytest.mark.parametrize("found", [[], [{"line": 2, "change": "Gt -> GtE"}], None])
def test_the_confirm_run_records_what_it_could_not_reproduce(
        tmp_path, monkeypatch, via, flag, found):
    def behave(side, prime, argv):
        return _report(unreproduced=found), _calls(), 0

    # Warm too: the confirm run is one cold run whatever the unit.
    session, engine, case = _engine_session(tmp_path, monkeypatch, behave, via=via,
                                            unit="warm")
    session.confirm(case, 4)
    argv, = engine.argvs
    assert flag in argv
    assert argv[argv.index("--mutate-workers" if via == "pytest" else "--workers")
                + 1] == "4"
    assert engine.seen == [False]
    assert session.confirmed == [{"case": "C", "workers": 4, "status": "ok",
                                  "unreproduced": found}]
    row, = _rows(tmp_path / "out" / "runs.tsv")
    assert (row["pair"], row["side"], row["unreproduced"]) == (
        "confirm", "after", "not said" if found is None else str(len(found)))


def _ended(differ="equal", failures=(), confirmed=(), status="ok"):
    return argparse.Namespace(differ={"C": differ}, process_failures=list(failures),
                              confirmed=list(confirmed), out="out",
                              runs=[argparse.Namespace(status=status)])


@pytest.mark.parametrize("session, rows, status", [
    (_ended(), [], 0),
    (_ended(differ="load only"), [], 0),
    (_ended(confirmed=[{"unreproduced": []}]), [], 0),
    (_ended(status="died"), [], 1),
    (_ended(), "inconclusive", 1),
    (_ended(confirmed=[{"unreproduced": [{"line": 1}]}]), [], 5),
    (_ended(confirmed=[{"unreproduced": None}], status="died"), [], 5),
    (_ended(failures=["counts"], confirmed=[{"unreproduced": None}]), [], 4),
    (_ended(differ="DIFFER", failures=["counts"]), [], 3),
])
def test_the_exit_status_says_the_worst_that_happened(session, rows, status, capsys):
    """Each failed check has its own status, so a script can tell a broken
    verdict from a slow machine; the most serious is the one given."""
    if rows == "inconclusive":
        rows = [_row(kept=1)]
    assert compare.exit_status(session, rows) == status


def test_the_new_checks_are_refused_where_they_cannot_hold(tmp_path, capsys):
    """A re-run is a warm slot's second run, and one worker confirms
    nothing: either asked otherwise would pass by never being checked."""
    base = ["HEAD", "HEAD", "--repo", ROOT, "--out", str(tmp_path / "out")]
    assert compare.main(base + ["--expect-rerun-processes"]) == 2
    assert "--unit warm" in capsys.readouterr().err
    assert compare.main(base + ["--confirm-run", "--workers", "1"]) == 2
    assert "above 1" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_the_finding_names_the_unit_the_processes_and_the_confirm_run():
    rows = [_row(processes_before=41, processes_after=41)]
    cold = _joined(compare.finding(_info(), rows))
    assert "each run cold" in cold
    assert "41 / 41 |" in compare.finding(_info(), rows)
    assert "Its sides started 41 and 41 pytest processes." in cold
    warm = _joined(compare.finding(_info(unit="warm", confirm=[
        {"case": "R40", "workers": 4, "status": "ok", "unreproduced": []}]),
        [_row(processes_before=41, processes_after=12, processes_differ=True,
              rerun="ok")]))
    assert "an untimed run from a fresh `.invective`, then the timed run" in warm
    assert "in some pair the two differed" in warm
    assert "as many as expected of a re-run" in warm
    assert "The R40 run at N = 4 with `--confirm` named no kill" in warm
    missed = _joined(compare.finding(_info(confirm=[
        {"case": "R40", "workers": 4, "status": "ok",
         "unreproduced": [{"line": 7, "change": "Lt -> LtE"}]}]), [_row(rerun="missed")]))
    assert "named 1 kill(s) it could not reproduce: line 7 Lt -> LtE" in missed
    assert "did not start the baselines" in missed


# --------------------------------------------------------------------------
# The documentation's command lines


def _bench_lines():
    found = []
    for name in sorted(os.listdir(os.path.join(ROOT, "docs"))):
        if not name.endswith(".md"):
            continue
        with open(os.path.join(ROOT, "docs", name), encoding="utf-8") as fh:
            text = fh.read().replace("\\\n", " ")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("uv run bench/compare.py "):
                found.append(line.split()[3:])
    return found


def test_the_docs_bench_commands_parse_and_name_cases_that_exist():
    """An example the owner copies must run as written: a flag renamed, or
    a case dropped from the file, leaves one that fails."""
    lines = _bench_lines()
    assert lines, "the docs show the command"
    _projects, cases = compare.load_cases(compare.CASES)
    for argv in lines:
        args = compare.parser().parse_args(argv)
        names = [n for item in args.case for n in item.split(",")]
        assert set(names) <= set(cases), argv


# --------------------------------------------------------------------------
# Whole sessions


def _git_checkout():
    return (shutil.which("git") and shutil.which("uv")
            and subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                               capture_output=True).returncode == 0)


sessions = pytest.mark.skipif(not _git_checkout(),
                              reason="a session needs git, uv and a git checkout")


def _project(tmp_path, slow=False, fetched=False):
    """A project of one module and its tests, and a cases file naming it.
    Both its mutants are killed, unless `FIXTURE_LAX` is set in the runs'
    environment, when the comparison's survives. One test always fails, and
    the project deselects it: a run it reached would be refused. *fetched*
    makes it a git repository the session fetches by its commit, as it does
    a project on a server."""
    where = tmp_path / "fixture"
    (where / "tests").mkdir(parents=True)
    (where / "pyproject.toml").write_text("[tool.pytest.ini_options]\n",
                                          encoding="utf-8")
    (where / "gate.py").write_text(
        "def admit(age):\n"
        "    if age < 18:\n"
        "        raise ValueError('under age')\n"
        "    return age\n", encoding="utf-8")
    (where / "tests" / "test_gate.py").write_text(
        "import os\n"
        "import time\n"
        "\n"
        "import pytest\n"
        "\n"
        "from gate import admit\n"
        "\n"
        "def test_admit():\n"
        "    time.sleep(%d)\n"
        "    if not os.environ.get('FIXTURE_LAX'):\n"
        "        assert admit(18) == 18\n"
        "    with pytest.raises(ValueError):\n"
        "        admit(10)\n"
        "\n"
        "def test_never():\n"
        "    assert False\n" % (120 if slow else 0), encoding="utf-8")
    source = 'path = "fixture"\n'
    if fetched:
        git(str(where), "init", "-q")
        commit(str(where))
        source = 'repo = "%s"\ncommit = "%s"\n' % (
            where.as_uri(), git(str(where), "rev-parse", "HEAD").strip())
    cases = tmp_path / "cases.toml"
    cases.write_text(
        "[projects.fixture]\n"
        + source +
        'deselect = ["tests/test_gate.py::test_never"]\n'
        "%s"
        "\n[cases.F]\n"
        'project = "fixture"\n'
        'target = "gate.py"\n'
        'tests = ["tests/test_gate.py"]\n'
        'only = "CMP,RAISE"\n' % ("calibrate = []\n" if slow else
                                  'calibrate = ["tests/test_gate.py"]\n'
                                  "calibrate_runs = 1\n"), encoding="utf-8")
    return cases


def _argv(tmp_path, cases, *more):
    return [sys.executable, os.path.join(BENCH, "compare.py"), "HEAD", "HEAD",
            "--cases-file", str(cases), "--case", "F", "--quick",
            "--python", sys.executable, "--repo", ROOT,
            "--out", str(tmp_path / "out"), "--cache", str(tmp_path / "cache"), *more]


def _worktrees():
    said = subprocess.run(["git", "-C", ROOT, "worktree", "list", "--porcelain"],
                          capture_output=True, text=True, check=True).stdout
    return {line.split(" ", 1)[1] for line in said.splitlines()
            if line.startswith("worktree ")}


def _left_behind(tmp_path):
    """The session's temporaries still there: its work directory, made in
    the test's own temporary directory, and its worktrees."""
    found = [name for name in os.listdir(tmp_path) if name.startswith("invective-bench-")]
    found += [w for w in _worktrees() if "invective-bench-" in w
              and os.path.normcase(str(tmp_path)) in os.path.normcase(w)]
    return found


def _rows(path):
    with open(path, encoding="utf-8") as fh:
        head, *body = [line.rstrip("\n").split("\t") for line in fh]
    return [dict(zip(head, row)) for row in body]


@sessions
def test_a_quick_session_measures_one_pair_and_leaves_nothing_behind(tmp_path):
    """The whole tool end to end on a project that takes seconds: the
    worktrees, the venvs, the calibrations, one pair, the verdicts, the
    files it writes, and the cleanup."""
    cases = _project(tmp_path, fetched=True)
    # A calibration a fraction of a second long, beside the suite's other
    # workers, drifts past any threshold a real one is held to. A survivor
    # on both sides fails the after side's run, by its setting alone.
    done = subprocess.run(_argv(tmp_path, cases, "--env", "FIXTURE_LAX=1",
                                "--set-after", "fail-on-survivors=true",
                                "--threshold", "1000"),
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    assert compare.ADVICE in done.stdout.splitlines()[0]
    out = tmp_path / "out"
    runs = _rows(out / "runs.tsv")
    assert [r["side"] for r in runs] == ["before", "after"]
    assert {r["status"] for r in runs} == {"ok"}
    assert {(r["unit"], r["prime_wall"]) for r in runs} == {("cold", "")}
    assert [(r["killed"], r["survived"], r["exit"]) for r in runs] == [
        ("1", "1", "0"), ("1", "1", "1")]
    assert all(float(r["wall"]) > 0 and r["cal_before"] and r["cal_after"] for r in runs)
    pairs = _rows(out / "pairs.tsv")
    assert [p["status"] for p in pairs] == ["kept"]
    assert pairs[0]["touchable"] != ""
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert re.search(r"^F +1 +1/1 .* equal$", summary, re.M), summary
    finding = (out / "finding.md").read_text(encoding="utf-8")
    assert finding.startswith("> **Finding:** (")
    assert "`fail-on-survivors = true`" in _joined(finding)
    assert "`FIXTURE_LAX=1`" in _joined(finding)
    assert "`tests/test_gate.py::test_never`" in _joined(finding)
    assert (tmp_path / "cache" / "fixture.git").is_dir()
    assert not _left_behind(tmp_path)


@sessions
def test_a_warm_session_with_every_check_and_a_confirm_run(tmp_path):
    """Warm slots, both process checks and the confirm run, end to end. The
    engine at HEAD remembers nothing, so its re-run starts every process its
    untimed run did: the sides agree, the re-run's count is missed, and the
    session fails on that alone."""
    cases = _project(tmp_path)
    done = subprocess.run(_argv(tmp_path, cases, "--unit", "warm", "--workers", "2",
                                "--same-processes", "--expect-rerun-processes",
                                "--confirm-run", "--threshold", "1000"),
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 4, done.stdout + done.stderr
    assert "not the 3 expected of a re-run" in done.stderr
    assert "before started" not in done.stderr
    out = tmp_path / "out"
    runs = _rows(out / "runs.tsv")
    assert [(r["pair"], r["side"], r["unit"]) for r in runs] == [
        ("1", "before", "warm"), ("1", "after", "warm"), ("confirm", "after", "cold")]
    # Three runs of the original (the first copy's twice, as two workers
    # make it) and two mutants: five processes, every time; a re-run that
    # remembered would start the three and nothing for the two kills, which
    # were not by time.
    assert [(r["prime_runs"], r["runs"]) for r in runs[:2]] == [("5", "5")] * 2
    assert all(r["prime_status"] == "ok" and r["prime_killed"] == "2" for r in runs[:2])
    assert runs[2]["unreproduced"] == "0"
    pair, = _rows(out / "pairs.tsv")
    assert (pair["same_processes"], pair["expected_processes"]) == ("True", "3")
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "5/5 rerun missed" in summary
    assert "F N=2 with --confirm (ok): unreproduced []" in summary
    finding = _joined((out / "finding.md").read_text(encoding="utf-8"))
    assert "The F run at N = 2 with `--confirm` named no kill" in finding
    assert not _left_behind(tmp_path)


@sessions
def test_verdicts_that_differ_fail_the_session_loudly(tmp_path):
    """A speedup that changes a verdict is a broken speedup, whatever its
    time: the difference is named as it is found, and the session fails."""
    cases = _project(tmp_path)
    done = subprocess.run(_argv(tmp_path, cases, "--env-after", "FIXTURE_LAX=1",
                                "--threshold", "1000"),
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "VERDICTS DIFFER" in done.stderr
    assert re.search(r"gate\.py:2 CMP Lt -> LtE: killed .* in run 1 .*survived in run 2",
                     done.stderr), done.stderr
    out = tmp_path / "out"
    assert "Lt -> LtE" in (out / "verdicts.txt").read_text(encoding="utf-8")
    assert re.search(r"^F .* DIFFER$", (out / "summary.txt").read_text(encoding="utf-8"),
                     re.M)
    assert "verdicts differ" in _joined((out / "finding.md").read_text(encoding="utf-8"))
    assert not _left_behind(tmp_path)


@sessions
@pytest.mark.skipif(os.name == "nt", reason="sends POSIX signals")
@pytest.mark.parametrize("how", ["a ^C at the terminal", "a SIGTERM"])
def test_a_stopped_session_records_its_run_and_removes_its_temporaries(tmp_path, how):
    """A ^C reaches the whole foreground group, the run included; a SIGTERM
    reaches the tool alone, which passes it on. Either way the run ends,
    is recorded as stopped, and nothing is left behind."""
    cases = _project(tmp_path, slow=True)
    proc = subprocess.Popen(_argv(tmp_path, cases), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        log = tmp_path / "out" / "logs" / "001-F-n1-before.log"
        deadline = time.monotonic() + 300
        while not (log.exists() and log.stat().st_size > 0):
            assert proc.poll() is None, proc.stdout.read().decode()
            assert time.monotonic() < deadline, "the first run never started"
            time.sleep(0.2)
        # Inside its baseline, a test that sleeps.
        time.sleep(3)
        sent = time.monotonic()
        if how == "a SIGTERM":
            proc.send_signal(signal.SIGTERM)
        else:
            os.killpg(proc.pid, signal.SIGINT)
        said = proc.communicate(timeout=120)[0].decode()
        # The run ended by the signal, not by the grace given a run that
        # did not hear it.
        assert time.monotonic() - sent < compare.GRACE, said
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    assert proc.returncode == 130, said
    runs = _rows(tmp_path / "out" / "runs.tsv")
    assert [r["status"] for r in runs] == ["stopped"]
    assert not _left_behind(tmp_path)
