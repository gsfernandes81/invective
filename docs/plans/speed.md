# Speed options

Option 4 for faster runs without losing the fresh-process guarantee;
planned.

## Intent

We intend to build option 4 below. We are open to dropping it if the code
complexity is not worth the return: measure on a real project before and
after, and cut it if it does not pay for itself.

## Options

Reference case for the estimates: a 5k-line project, 600 tests, a 20 s
serial suite, 8 cores (about 6 effective workers), 400 mutants, 85%
killed, 0.5 s fixed cost per run. Serial and unaided that is about 80
minutes. All multipliers are estimates, not measurements.

| # | Option | Estimated speedup | Risk to correctness |
|---|---|---|---|
| 4 | Cache verdicts, keyed on the mutated module's text, the ordered selection, every test file and `conftest.py`, pytest config, interpreter and distribution versions, and the repository files the baseline imported | 1x on a first run; 10-50x on a sweep where one module changed | Unsound for files only a child interpreter imports. Timeouts are never cached; cached verdicts are marked in the report. Opt-in. |

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
