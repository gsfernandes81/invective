# Speed options

Options 3 and 4 for faster runs without losing the fresh-process guarantee;
planned.

## Intent

We intend to build options 3 and 4 below. We are open to dropping either
of them if the code complexity is not worth the return: measure on a real
project before and after each one, and cut the ones that do not pay for
themselves.

## Options

Reference case for the estimates: a 5k-line project, 600 tests, a 20 s
serial suite, 8 cores (about 6 effective workers), 400 mutants, 85%
killed, 0.5 s fixed cost per run. Serial and unaided that is about 80
minutes. All multipliers are estimates, not measurements.

| # | Option | Estimated speedup | Risk to correctness |
|---|---|---|---|
| 3 | Previous killer as a probe: the test that killed a mutant last time, run alone once it passes alone on the unmutated copy; otherwise the full selection | About 3.4x on a re-run with a prior report, 1x on a first run | A kill the full selection's order would hide counts, and is marked in the report. |
| 4 | Cache verdicts, keyed on the mutated module's text, the ordered selection, every test file and `conftest.py`, pytest config, interpreter and distribution versions, and the repository files the baseline imported | 1x on a first run; 10-50x on a sweep where one module changed | Unsound for files only a child interpreter imports. Timeouts are never cached; cached verdicts are marked in the report. Opt-in. |

## How they combine

- On a **first run** (CI, a fresh clone, new mutants) 3 and 4 have no
  history, so a kill still costs about half the suite.
- On a **warm re-run** with a prior report, 3 makes a kill cost one test.
- **Survivors are the floor in every case.** Nothing in 3 helps them: a
  survivor has no killer.
- With 4, a re-run after a small change takes under a minute: every
  verdict but those of the changed module's mutants is read from the cache.

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
