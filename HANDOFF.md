# Handoff: speeding invective up without giving up its guarantees

Findings from a design review (2026-10-01). Nothing here is implemented. The
estimates were made against the git-worktree engine; `main` has since moved to
copies of the working tree (`tree.py`) and a `[tool.invective]` table
(`config.py`), and the section on that move below has been checked against
them. The speedup options themselves (workers, coverage, killer-first, cache)
are still all to do.

## Intent

All four options below are approved by the owner (2026-10-07), and they are
the current scope of work. We are still open to dropping any of them if the
code complexity is not worth the return: measure on a real project before and
after each one, and cut the ones that do not pay for themselves.

## How the work is to be done (owner, 2026-10-07)

1. **Plan.** A Fable advisor drafts the implementation plan; a second Fable
   advisor reviews it; the two loop, draft and review, until they converge.
2. **Build.** Claude implements the converged plan, bringing blockers and
   issues to the owner as they come up and carrying on with everything not
   blocked, until finished or blocked on every front. A reminder timer is set
   at the start of this phase to restate that rule, and kept alive (re-armed
   each time it fires) until the phase ends.
3. **Review.** An Opus agent reviews the code. A Fable advisor sorts its
   findings into must fix, can fix and don't fix (incorrect findings), and
   says how to fix each. Claude then fixes them.

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
| 3 | Previous killer as a probe: run the test that killed a mutant last time alone first, and the full selection only if it passes (decided 2026-10-07, see Decisions) | About 3.4x on a re-run with a prior report, 1x on a first run | Moving the killer to the front, the first idea, was not risk-free: an order-dependent test can fail on the original and score a false kill. The probe is gated on the killer passing alone on the original. |
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
  skipped, which changes process and order. Decided, reversibly: see
  Decisions below.
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

## Decisions

- **2's survivor confirmation re-runs the full selection** (owner, 2026-10-07).
  Not only the tests the subset skipped: running those alone changes the
  process and the test order a survivor is judged in. **This is reversible and
  should be raised again whenever performance is the concern**, since survivors
  are the floor of every run and this choice keeps each one at the cost of the
  whole selection. The alternative to weigh then is the skipped tests alone,
  against a measurement of how often the two disagree on a real project.

- **3 is a probe, not a reorder** (proposed in this session, reviewed by an
  independent advisor, who accepted it with the amendments folded in below,
  2026-10-07). Moving the previous killer to the front changes which tests
  run before it and before everything it jumps; an order-dependent test then
  fails on the original and scores a false kill, the silent direction.
  - *Gate.* After the full baseline and before the first mutant is written,
    each distinct remembered killer K is run alone on the unmutated copy,
    with the mutant budget as its timeout (not `BASELINE_TIMEOUT`). K is
    usable as a probe in this run only if that run is `ok` with nothing
    `missing`; otherwise it is dropped without a refusal, since the full
    selection still decides. On the plugin path, killers not in the
    selection are skipped before the gate.
  - *Probe.* Per mutant with a usable K: run K alone (a one-line selection
    file; on the CLI path the `--tests` arguments are kept, since they can be
    `-k`, `-m` or a directory, which the probe then still collects). It is a
    kill only when `code == TESTS_FAILED`, `killer == K` and nothing is
    `missing`. Anything else, a pass, a collection error, exit 2 to 5 or a
    timeout, falls back to the full selection in its original order, exactly
    as today, so `broken` and kills by time are unchanged.
  - *A probe kill is a definition change and is marked.* K alone can kill a
    mutant the full run would let survive (an earlier test warms a cache so K
    never reaches the mutated branch). That is still a real kill: K is in the
    selection and passes alone on the original. The kill carries
    `"via": "probe"`, and `summary()` counts probe kills on a line of their
    own beside the timeout and broken lines.
  - *History.* `.invective/history.json` at the project root, the directory
    created with a `.gitignore` reading `# Created by invective
    automatically.` then `*`, as pytest does for `.pytest_cache`. Shape:
    `{target_rel_path: {mutant_key: killer}}`. Read and written inside
    `mutate()`, so `invective run` and `pytest --mutate` both use it.
    Written atomically (temp file and replace). A new kill overwrites; a
    survivor, accepted or not, deletes its entry; mutants not run this time
    (`--only`, `--limit`) keep theirs; keys no longer among the module's full
    set of sites (from `every`, not the filtered list) are pruned. Only
    probeable killers are stored: a node id with `::` that is not `TIMEOUT`
    (never a collection kill's bare path, never an empty killer). A corrupt
    or unreadable history is ignored with a warning, not a refusal. On by
    default, since it cannot change a verdict other than as marked above;
    `history = false` in `[tool.invective]` turns it off. `.invective` joins
    `tree.py`'s skip list. CI can persist it with actions/cache. Option 4's
    cache would live in the same directory.
  - *Matching.* Key: JSON of `[kind, change, ast.unparse(node), stripped
    source line, ordinal]`, the ordinal counting sites with an identical
    tuple in `_sites` order (`ast.walk`'s, already deterministic). No
    enclosing qualname: the ordinal breaks the same ties without a parent
    map. A wrong match costs speed only.
  - *Measure* the gate's cost (one start per distinct killer, about 0.25 to
    1.5 s each) on a real project; a heavy `conftest.py` is where it shows.

- **Resuming an interrupted run is part of option 4** (owner, 2026-10-07).
  Issue #1's "Resume an interrupted run" item was deferred from the work on
  that issue to be built here. A long sweep killed partway through should
  skip the mutants whose verdicts it already has. Record each verdict as it
  lands, not at the end, so a killed run keeps what it measured. The cache's
  key rules apply unchanged: never resume across a changed target, selection,
  config or interpreter, and never resume a timeout. It may be the first
  slice of option 4 to land. Done, per the issue, when a run killed partway
  through and started again skips the mutants it already has, and a change to
  the source or the tests invalidates them.

## Open items

- Measure 1 first, on a real project; it is the largest and the cheapest.
- Worth a README line either way: a child interpreter started in a different
  directory imports whatever copy of the package is installed, not the one
  under test, unless it is installed editable or the test puts the copy on
  `sys.path`.
