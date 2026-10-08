# Speed options

Options 1-4 for faster runs without losing the fresh-process guarantee;
planned, none built.

## Intent

We intend to build options 1 to 4 below. We are open to dropping any of
them if the code complexity is not worth the return: measure on a real
project before and after each one, and cut the ones that do not pay for
themselves.

## Options

Reference case for the estimates: a 5k-line project, 600 tests, a 20 s
serial suite, 8 cores (about 6 effective workers), 400 mutants, 85%
killed, 0.5 s fixed cost per run. Serial and unaided that is about 80
minutes. All multipliers are estimates, not measurements.

| # | Option | Estimated speedup | Risk to correctness |
|---|---|---|---|
| 1 | Workers: one copy and one process group per worker, mutants taken from a shared queue, results printed in site order | 5-7x on 8 cores | None. Parallel tests that share a port, a `/tmp` path or a database can interfere; a repository whose `addopts` already carries `-n` has declared itself safe. A refusal or ^C must stop every live run. |
| 2 | Coverage-guided selection: run only the tests that cover the mutated lines, then re-run every survivor against the full selection before reporting it | 2-8x, about 3x on the reference case; about 2.6x on a first run | A survivor is still one the whole selection let through. Falls back to the full selection when coverage is missing or a line has no covering test. Opt-in, `coverage` as an optional extra. |
| 3 | Previous-killer-first: put the test that killed a mutant last time first in its selection | About 3.4x on a re-run with a prior report, 1x on a first run | None. Same tests, different order. |
| 4 | Cache verdicts, keyed on the mutated module's text, the ordered selection, every test file and `conftest.py`, pytest config, interpreter and distribution versions, and the repository files the baseline imported | 1x on a first run; 10-50x on a sweep where one module changed | Unsound for files only a child interpreter imports. Timeouts are never cached; cached verdicts are marked in the report. Opt-in. |

## How they combine

- On a **first run** (CI, a fresh clone, new mutants) 3 and 4 have no
  history, so a kill still costs about half the suite. 2 is worth about
  2.6x on top of 1+3+4 there.
- On a **warm re-run** with a prior report, 3 already makes a kill cost
  one test, so 2 adds about nothing.
- **Survivors are the floor in every case.** Nothing in 2 or 3 helps
  them: a survivor has no killer, and 2's confirmation pass puts it back
  at the full suite. Re-running the full selection is safer than running
  only the tests the subset skipped, which changes process and order.
- **4 is weaker without 2.** The whole module's text is in the key, so
  any edit to a module invalidates all of its mutants; what is left is the
  sweep case, modules that did not change. Saying which mutants a changed
  test affects needs option 2's coverage map.
- `invective sweep` already narrows to the test files that import the
  module, which makes 2's gain smaller there.
- Recommended stack, first run: about 5.5 minutes against about 80
  (roughly 8-25x), or about 14.5 minutes without option 2's coverage
  selection. A cached re-run after a small change: under a minute.

If most runs are warm local re-runs, 1+3+4 is likely enough to start. If
CI is the main use, 2 is the one worth building.

> **Finding:** (2026-10-01, this repo, 4 cores, Python 3.11, pytest 9.1.1)
>
> A fresh pytest process costs about 0.23-0.30 s (`python -c pass` 14 ms,
> `import pytest` 173 ms, pytest on one trivial test 228 ms). A real
> `conftest.py` can push that to 0.5-1.5 s. The engine runs the 6-mutant
> fixture in 0.37 s per run. Copying a 224 KB tree: `shutil.copytree`
> 6 ms, `git worktree add` 14 ms (a wash at this size; both grow with
> the tree's bytes). invective's own suite is the opposite
> case: each test spawns pytest, 0.2-3.5 s per test, so startup is under
> 10% of a run and option 2 would be worth 10-20x.

## Open

- Decide whether 2's survivor confirmation re-runs the full selection or
  only the tests the subset skipped.
- Decide where a prior report lives for 3, and how mutants are matched
  across runs (by kind, change and source line text, not line number).
- Measure 1 first, on a real project; it is the largest and the cheapest.
