# Handoff: speeding invective up without giving up its guarantees

Findings from a design review (2026-10-01). Nothing here is implemented. The
estimates were made against the git-worktree engine; `main` has since moved to
copies of the working tree (`tree.py`) and a `[tool.invective]` table
(`config.py`), and the section on that move below has been checked against
them. The speedup options themselves (workers, coverage, killer-first, cache)
are still all to do.

## Intent

We intend to build options 1 to 4 below. We are open to dropping any of them
if the code complexity is not worth the return: measure on a real project
before and after each one, and cut the ones that do not pay for themselves.

## Where we differ from pytest-gremlins

pytest-gremlins is fast because it instruments a module once and switches
mutants on with an environment variable, so Python is never restarted. Read
from its source:

- It serves the instrumented module from an import hook inside the pytest
  process and writes nothing at the module's path. A child interpreter that a
  test starts imports the original file, so the mutant is invisible to it and
  survives silently.
- Its coverage pre-scan does not record subprocess coverage.
- Its cache key is the mutant, the source file and the covering test files; any
  other module the tests import is not in it.

invective writes each mutant to disk and runs a fresh pytest process, so a
test that spawns a CLI, a worker or a child interpreter sees the mutant. Most
of gremlins' speed is the speed of not restarting Python, and that is the part
that conflicts with this. We do not take it.

## Options

Reference case for the estimates: a 5k-line project, 600 tests, a 20 s serial
suite, 8 cores (about 6 effective workers), 400 mutants, 85% killed, 0.5 s
fixed cost per run. Serial and unaided that is about 80 minutes. **All
multipliers are estimates, not measurements.**

| # | Option | Estimated speedup | Risk to correctness |
|---|---|---|---|
| 1 | Workers: one copy and one process group per worker, mutants taken from a shared queue, results printed in site order | 5-7x on 8 cores | None. Parallel tests that share a port, a `/tmp` path or a database can interfere; a repository whose `addopts` already carries `-n` has declared itself safe. A refusal or ^C must stop every live run. |
| 2 | Coverage-guided selection: run only the tests that cover the mutated lines, then re-run every survivor against the full selection before reporting it | 2-8x, about 3x on the reference case; about 2.6x on a first run | A survivor is still one the whole selection let through. Falls back to the full selection when coverage is missing or a line has no covering test. Opt-in, `coverage` as an optional extra. |
| 3 | Previous-killer-first: put the test that killed a mutant last time first in its selection | About 3.4x on a re-run with a prior report, 1x on a first run | None. Same tests, different order. |
| 4 | Cache verdicts, keyed on the mutated module's text, the ordered selection, every test file and `conftest.py`, pytest config, interpreter and distribution versions, and the repository files the baseline imported | 1x on a first run; 10-50x on a sweep where one module changed | Unsound for files only a child interpreter imports. Timeouts are never cached; cached verdicts are marked in the report. Opt-in. |

### How they combine

Corrected after a second look at the overlap between 2 and 3:

- On a **first run** (CI, a fresh clone, new mutants) 3 and 4 have no history,
  so a kill still costs about half the suite. 2 is worth about 2.6x on top of
  1+3+4 there (about 14.5 min against about 5.5 min on the reference case).
- On a **warm re-run** with a prior report, 3 already makes a kill cost one
  test, so 2 adds about nothing.
- **Survivors are the floor in every case.** Nothing in 2 or 3 helps them: a
  survivor has no killer, and 2's confirmation pass puts it back at the full
  suite. The full-suite re-run is safer than running only the tests the subset
  skipped, which changes process and order.
- **4 is weaker without 2.** The whole module's text is in the key, so any edit
  to a module invalidates all of its mutants; what is left is the sweep case,
  modules that did not change. Saying which mutants a changed test affects
  needs 2's coverage map.
- `invective sweep` already narrows to the test files that import the module or
  are named after it, which makes 2's gain smaller there than the reference
  case suggests.
- Recommended stack, first run: about 5.5 minutes against about 80 (roughly
  8-25x). A cached re-run after a small change: under a minute. Both are
  estimates.

If most runs are warm local re-runs, 1+3+4 is likely enough to start. If CI is
the main use, 2 is the one worth building.

### Measured (4 cores, Python 3.11, pytest 9.1.1, this repo)

- A fresh pytest process costs about 0.23-0.30 s (`python -c pass` 14 ms,
  `import pytest` 173 ms, pytest on one trivial test 228 ms). A real
  `conftest.py` can push that to 0.5-1.5 s.
- The engine runs the 6-mutant fixture in 0.37 s per run.
- Copying this 224 KB tree: `shutil.copytree` 6 ms, `git worktree add` 14 ms.
  A wash at this size; both grow with the tree's bytes.
- invective's own suite is the opposite case: each test spawns pytest, 0.2-3.5
  s per test, so startup is under 10% of a run and option 2 would be worth
  10-20x.

## Not to build

Each of these trades the on-disk, fresh-process guarantee for a speedup, and
fails in the silent direction invective exists to refuse.

- Mutation switching by environment variable or in-process, warm pytest pools
  and fork servers: at most 2x on kills and 1x on survivors once 2 is in, and
  they miss import-time mutants, leak state between mutants, and do not work on
  Windows.
- A lightweight runner that calls test functions directly.
- Coverage selection without the confirmation re-run of survivors.
- A cache keyed on the target and the tests only.

### `PYTHONDONTWRITEBYTECODE` in mutant runs: measured, left out

Cosmic Ray runs its tests with `PYTHONDONTWRITEBYTECODE=1`, and issue #1 asked
whether invective should too, as a second guard behind the mtime stamping
against a mutant inheriting the previous mutant's `.pyc`. The owner decided
against it (2026-10-07) on these numbers, taken on a copy of or3 (4 cores,
nothing else running; the variable was the only difference):

| or3 benchmark | mutants | off, mean | on, mean | cost |
|---|---|---|---|---|
| `run` on `cfg.py`, `--only RAISE,CMP,NOT --limit 20`, 3 runs each | 18 | 47.5 s | 53.4 s | +12% |
| `run` on `tools/farpy.py`, all kinds, 3 runs each | 19 | 19.1 s | 20.3 s | +6% |
| `sweep` of `cfg.py`, `tools/farpy.py`, `logins.py`, 2 runs each | 22 | 252.6 s | 295.0 s | +17% |

Verdicts and survivors were identical on and off in every run (`cfg.py` 7/18,
`farpy.py` 9/19). Only the sweep separates clearly; the two `run` rows are
within noise. The cost is every module a run imports other than the mutant,
recompiled on every run, so it grows with how much a test imports, and is
never negative. The stamping already holds on the file where or3's old engine
went 9/11/9 of 19, and the copy carries no `__pycache__`, so a second guard
that costs 6-17% of every run is not worth it. Worth revisiting only if a
filesystem whose mtimes the stamping cannot rely on turns up.

## Moving from git worktrees to copies

Landed on `main` (`tree.py`). No earlier verdict flips. What the move means
for the options above, checked against that code:

- "Measures the last commit" became "measures the working tree as it stands",
  with a commit still available. An uncommitted test now counts. The cost is
  drift: an edit saved mid-run is not seen. Hash the target and the selection at
  the start, and say at the end if they changed.
- "Exactly one edit from the original" still holds: the copy is the original
  and the per-mutant write is still the reset.
- The copy is per worker, not per mutant (relevant to option 1). Never
  hardlink: rewriting the target would write through to the checkout.
- Already skipped: `.git`, `.hg`, `.svn`, `.tox`, `.nox`, `__pycache__`,
  `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `node_modules`, and any
  directory holding a `pyvenv.cfg`. `__pycache__` and `.pytest_cache` matter for
  correctness, not size: a copied `.pyc` whose mtime is in the future and whose
  size matches a mutant's would run the original's bytecode, and a copied
  `.pytest_cache` leaks `--lf` state into the runs.
- **Symlinks are copied as symlinks** (`symlinks=True`). One inside the tree
  that points back at the checkout (an absolute target, or `../` out of the
  tree) makes the tests import the original and the mutant survive silently.
  Detect it and refuse, or leave it to the editable-install check below.
- An editable install points at the checkout. The plugin already runs inside
  every run; at session end it can check that the target's `__file__` is under
  the copy and refuse if not. A child interpreter started in another directory
  is still a documented hole, as it was before.
- On Windows: symlinks need a privilege, deep `node_modules` trees hit
  `MAX_PATH`, and a killed run holding a file can still break removal.
- A SIGKILLed run leaks its copy, which may hold a copied `.env`. Write an
  owner-pid marker and sweep dead copies at startup.

### Gitignored files: two keys not yet there

`[tool.invective]` has `exclude` (globs of anything to leave out) but nothing
that follows `.gitignore`. Proposed, in the same flat table and with the same
rule that an unknown or ill-typed key is a refusal:

```toml
[tool.invective]
ignore-gitignored = false   # default: copy everything except what is skipped above
include-ignored   = [".env", "src/pkg/_version.py", "build/*.so"]   # repo-relative globs
```

`ignore-gitignored = true` skips every file git reports as ignored, using
`git ls-files --others --ignored --exclude-standard`, so it needs git and
refuses outside a repository. Untracked files that are not ignored are still
copied. `include-ignored` re-includes a subset of the ignored files and does
nothing when `ignore-gitignored` is false; `exclude` still wins over it. The
default is false because gitignored files are sometimes load-bearing (`.env`, a
generated `_version.py`, in-place `.so` builds, fixture data). A file that was
left out and was needed fails the baseline, which is a refusal and not a wrong
score, and its message should name the option.

## Open items

- Decide whether 2's survivor confirmation re-runs the full selection or only
  the tests the subset skipped.
- Decide where a prior report lives for 3, and how mutants are matched across
  runs (by kind, change and source line text, not line number).
- Measure 1 first, on a real project; it is the largest and the cheapest.
- Worth a README line either way: a child interpreter started in a different
  directory imports whatever copy of the package is installed, not the one
  under test, unless it is installed editable or the test puts the copy on
  `sys.path`.
