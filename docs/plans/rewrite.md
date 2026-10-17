# Rewrite

invective 1.0 built from nothing on a fork-after-collect engine: the decisions, their
reasons, the stages, and what done means; planned.

## The thesis

Every mutant's run today pays for an interpreter, pytest, the project's conftest and a
collection before the first test runs. That cost is the same for every mutant, it is
between half a second and two seconds on a real suite, and pytest-gremlins' own profile
puts it at about 90% of a run. invective pays it once, in a parent that never runs a
test, and runs each mutant in a child forked from that parent. Everything else in this
plan serves that one move or keeps it honest.

## How to use this plan

This plan was written by one agent after a long design conversation. It is a record of
decisions and the reasons behind them, not a specification, and it is written with room
for reality.

- **Any decision here may be overturned.** The reasons are given so that an agent can
  tell whether the reason still holds. If it does not, or if the implementation shows
  the decision was wrong, say so to the user and propose the change, with what you saw.
  Do not quietly work around a decision, and do not quietly keep one that is failing.
- **Subagents have the same standing.** An implementer or reviewer who finds that
  something does not add up tells the orchestrating agent what it thinks is wrong and
  why, and the orchestrating agent carries it to the user when it changes a decision.
  That channel has been useful before and is wanted.
- **The research is optional reading.** `docs/plans/rewrite-research.md` holds the
  measurements and the literature behind these decisions. Read the part that bears on a
  decision you are about to question. Nobody has to read it to build.
- **Granularity is deliberate.** The plan says what and why. File layout, module
  names, class shapes and exact constants are the implementers' to choose, within the
  invariants below.

## Decisions

### 1. What 1.0 is

In: the engine (below), two operators, coverage-guided selection, parallel children,
the subprocess slow path, accept comments with stale detection, `--changed-since`, a
console and a JSON report, exit codes a gate can use, `--fail-fast`, the forced-fail
canary, the verdict-equality harness, the benchmark against three public projects, docs,
version 1.0.0 and a pull request against `main`.

Out, with the reason in "Deferred" below: infection analysis, the verdict cache,
arid-node suppression, pooling, split-stream execution, a startup hook for interpreters
a test starts, invective's own mutation score, Windows as a tested platform, the Stryker
report schema.

### 2. The execution model

`pytest --mutate` is the engine's home. In the session the user started:

1. The import finder is installed at `pytest_load_initial_conftests`, ahead of pytest's
   assertion-rewrite hook, so a target imported by a conftest or a test module is served
   instrumented. A target already in `sys.modules` at that point was loaded by an earlier
   plugin and cannot be instrumented: refuse, naming the module, rather than report
   survivors that never switched.
2. Each target is instrumented once, with every mutant embedded. Instrumented code is
   compiled under the file's real path, keeps its line numbers, has `__file__`, and runs
   its original body when no mutant is active.
3. pytest collects as it always does. The session must not have xdist workers
   (`-n 0`); a controller with workers collects nothing.
4. The parent never runs a test. Child 0, forked from it, runs the whole selection once:
   that is the green-baseline check and the pre-scan in one run. A red baseline is a
   refusal.
5. Each mutant's tests run in a child forked from the parent, N children at a time, one
   mutant per child. A child sets the active mutant in a module global of every
   instrumented module (and in the environment, for any interpreter a test starts),
   redirects its stdout and stderr to files of its own, runs its covering tests through
   `pytest_runtest_protocol` in order, stops at the first failure, writes its verdict,
   and exits with `os._exit`.
6. `gc.freeze()` before the first fork.

Invariants the implementation must keep, whatever shape it takes:

- The parent is pristine: no test body, no fixture, no mutant ever runs in it.
- A child's verdict is data in a file, never an exit code. The child writes one JSON
  line per finished test and one for the verdict, so a killed child leaves a readable
  partial record. The parent reads after `waitpid`.
- The switch is a plain module-global read: one compare at function entry for a body
  replacement, an inline conditional for an operator. Never an environment lookup per
  call (measured at 68% slower by gremlins on more-itertools).
- A test that starts a second interpreter sees the original code in it. That is a known
  limit of 1.0, and such tests still run and still pass.

Why fork and not a subprocess per mutant: the cost above. Why not in-process: no way to
kill a hung mutant, state leaking between mutants, and fork is within about a
millisecond of it anyway. Why fork after collection and not before: mutmut 3 forks
before and pays a pytest session and a collection per mutant; that is the structural
difference this engine exists to remove.

### 3. The pre-scan

Child 0 maps tests to code with `sys.monitoring` (Python 3.12+), not with coverage.py:

- LINE events, with the callback returning DISABLE for every code object outside the
  targets at its first event, so after warm-up the tracer costs nothing outside the
  mutated modules. The first event still records which file the code object belongs to,
  so every file executed by a test is known (the verdict cache will need it; see
  "Deferred").
- Lines run during a test's call phase are attributed to that test. Lines run while a
  fixture is set up are attributed to every test that uses that fixture, not only the
  test that happened to trigger it, so code a session fixture calls is tested by the
  tests that depend on its result.
- Module-level code that runs at import or collection, in the parent before any test,
  is recorded against a pseudo-test. A mutant whose span ran there takes the slow path
  (decision 6).
- Per test: wall time and CPU time, by phase, for ordering and budgets.

Why not coverage.py: its sysmon core drops per-test contexts on 3.14, its pre-scan was
the single largest cost in gremlins' profile, and we need lines of the targets and file
membership of everything else, which a few dozen lines of monitoring code give directly.

### 4. Operators

Two genres, both scoped to function bodies, both never touching the
`if __name__ == "__main__"` guard. Module-level code is not mutated: its mutants would
run before any switch, and cargo-mutants does not mutate it either.

**`fn_value`**: the function body becomes a return of each value of its return
annotation, with the fork's type table copied as is (the file carries the MIT notice of
`pytest-gremlins`, from which the table comes). A generator becomes empty, then yields
one value. Constructors, `@abstractmethod`, `@overload` and empty bodies are skipped, as
the fork skips them.

**Binary operators**, with the subsumption-minimal tables for comparisons and booleans
(Kaminski, Ammann and Offutt; Just, Kapfhammer and Schweiggert) and the fork's tables
for the rest:

| original | replacements | why these |
|---|---|---|
| `a < b` | `a <= b`, `a != b`, `False` | each differs on exactly one region (`==`, `>`, `<`); every other replacement is killed by a test that kills one of these |
| `a > b` | `a >= b`, `a != b`, `False` | same, mirrored |
| `a <= b` | `a < b`, `a == b`, `True` | same |
| `a >= b` | `a > b`, `a == b`, `True` | same |
| `a == b` | `a <= b`, `a >= b`, `False` | regions `<`, `>`, `==` |
| `a != b` | `a < b`, `a > b`, `True` | regions `<`, `>`, `==` |
| `a and b` | `False`, `a`, `b`, `a == b` | the four truth-table cells |
| `a or b` | `True`, `a`, `b`, `a != b` | the four truth-table cells |
| arithmetic, bitwise, shifts, their `op=` forms | the fork's `binary_operator` table | no proven minimal set exists; two per site is a reasonable count |

`in`, `not in`, `is`, `is not`, `**` and `@` are not mutated, as in the fork.

Every mutant has a **structural id**: module, function qualname, the operator node's
path within the function, the replacement. Never a line number or a per-file counter,
so that ids survive edits above them and formatting, which the diff scope and the
future cache depend on.

**Trivial equivalence**: if comparing a mutant's compiled code object with the
original's and with its siblings' is a few dozen lines, do it, and drop equivalents and
duplicates before any run. If it grows beyond that, drop the feature; the expected gain
is small.

The dispatch shape is the implementers' choice (an inline conditional per site, or a
per-function trampoline with the mutants as separate functions as mutmut does), within
the invariant that the switch is one module-global read.

### 5. Selection and order

- A mutant's covering set is the tests that ran any line of its span in the pre-scan.
- Order: cheapest first by baseline duration, with one tiebreak ahead of it: a test that
  already killed a mutant in the same function during this run goes first.
- Stop at the first failure. The verdict is `killed` with that test's node id.
- A mutant no test reached is `unreached`: reported as a survivor, flagged, never run.
- `--fail-fast` stops the run at the first unaccepted survivor; the gate's answer is
  decided by then. Order mutants widest cover first under it, since those are the
  hardest to kill.

### 6. The slow path

A fresh `python -m pytest` with the finder installed by a small bootstrap, the switch
set in its environment, and the whole selection as its tests. Used for a mutant whose
span executed during import or collection (the fork cannot re-run that code), and as the
whole engine on Windows. Both paths produce the same verdict shape and the harness
asserts they agree.

### 7. Budgets, timeouts and the verdict vocabulary

Every budget is relative to a baseline measured on the same machine in the same run,
with a small floor and no absolute cap: a slow machine has a slow baseline. Two limits
per child, both multiples of the covering tests' baseline: `RLIMIT_CPU` (catches a busy
loop even when the machine is contended) and a wall budget enforced by the parent with
SIGKILL of the child's process group (catches a sleep, a deadlock, a blocked read).
Multipliers and floors are configurable; the implementers pick defaults and say why.

A timeout is never a verdict on its own. The mutant is re-run alone with nothing else
running; if it times out again, its selection is run unmutated under the same limit.
Only when the unmutated run finishes comfortably inside the limit is the timeout a kill.
gremlins measured a fake 42.9% score from contention before adopting this rule.

| verdict | meaning | counts as |
|---|---|---|
| `killed` | a covering test failed; the first is named | killed |
| `killed` by `timeout` | over budget alone, unmutated run fits inside the limit | killed |
| `survived` | every covering test passed | survivor |
| `accepted` | a survivor an accept comment names; still run | accepted |
| `stale` | an acceptance whose mutant was killed, or that names no mutant of the line | fails the project's rules |
| `unreached` | no test ran the span in the baseline | survivor, flagged |
| `unmeasured` | timed out alone and so did the unmutated run; or the child died without answering | neither; listed |

### 8. Proof that the mutant is live

- **Forced-fail canary**, before any mutant runs: activate a synthetic mutant that
  raises in every instrumented function and run the selection in a child; it must fail.
  This is the check whose absence let gremlins ship two executors that never ran
  mutated code.
- **Reached flag**: a child records whether the active mutant's code ever executed. A
  survivor whose site never executed is reported as `survived, site not reached`, not as
  a plain survivor: the map and the run disagreed, which points at order dependence or
  nondeterminism, and the verdict is not evidence about the mutant.

### 9. Refusals

Red baseline. Empty selection. A target imported before instrumentation. Threads alive
in the parent at fork time (a conftest started one; forking would deadlock children).
xdist workers on. Each refusal names its cause and exits 2.

### 10. Surface

- `pytest --mutate <path>...` and `pytest --mutate --changed-since <ref>`. With
  `--changed-since` and no paths, the targets are the Python files under the project
  whose functions changed against the ref, minus files pytest collected as test modules
  and conftests.
- `--changed-since` reads the base text with `git show ref:path` (git is the one source)
  and compares per-function AST hashes, positions dropped, docstrings, decorators,
  defaults and annotations kept; a mutant is in scope when its function's hash differs.
  Working-tree edits count. If the function-level comparison proves awkward, hunk lines
  are the fallback, and the plan should be updated to say so.
- `invective run ...` is a thin wrapper that execs `pytest --mutate` with the same
  options. `[tool.invective]` holds defaults; an unknown key is refused.
- Accept comments keep their form: `# invective: accept[reason: change] text`, reason
  one of `equivalent`, `untestable`, `out_of_scope`, the change optional and matched
  against the mutant's description; a comment above a line applies to it. Stale
  detection stays.
- Console report by default; `--json <path>` writes every mutant with its id, file,
  line, description, verdict, killer, budget and the acceptance it matched.
- Exit codes: 0 by default, survivors included, since a survivor is a finding; with
  `fail-on-survivors = true` in `[tool.invective]`, 1 for an unaccepted survivor, a
  stale acceptance or an unmeasured mutant; 2 a refusal; 4 a usage error. The gate
  needs nothing but the exit code and the console lines.
- Workers default to the CPU count.

### 11. Platforms and dependencies

Python 3.12 to 3.14. Linux and macOS first class. Windows runs through the slow path
and is untested here: the `checks` leg for it stays only if it is green at stage 4,
otherwise it is removed and the README says so. The only runtime dependency is pytest.

### 12. The verdict cache (deferred, principle fixed)

When it is built, it is content-addressed and has no invalidation logic: a lookup hits
or misses. The key for the pair (mutant, test) is the mutant's id and function hash,
plus the AST hashes of every function and module body the test executed in the pre-scan
(fixtures, helpers, hooks and callees are all executed code), plus the module-level
hashes of every project module imported in the parent before the first test (registries
filled at import time that any test may read), plus an environment stamp (interpreter,
lockfile, pytest configuration, operator table version). The pre-scan runs every time,
so test-side changes need no separate handling: a changed test has a changed closure and
misses. A dead file added or removed invalidates nothing. The structural ids and the
whole-project file membership in the pre-scan are the two things 1.0 does so that this
needs no migration.

### 13. Deferred, with reasons

- **Infection analysis** (weak mutation): a probe build of the targets, run only in
  child 0, records per site and per test which replacements would have changed the
  value; never-infected mutants are survivors without a run and infected ones run only
  the tests that infected them. Saves test bodies, so helps every suite. Deferred only
  because it is not needed to clear the bar; it is the first thing after 1.0.
- **Verdict cache**: decision 12.
- **Arid-node suppression**: skipping code whose mutants are reliably dismissed
  (logging, `__repr__`); seed it from a project's accept comments once there are some.
- **Pooling** (several mutants per child, disjoint covers): measured on or3 at under
  0.25% of run time for the fixture setup it could share, and unsound across mutants
  that share memoised state. Not to be built.
- **Split-stream execution** (fork at the first mutation site inside a test): the
  largest prefix-sharing in the literature and the most fragile; its target cost
  measured small on or3. A spike at most, after 1.0 has numbers.
- **Startup hook for interpreters a test starts** (gremlins' `sitecustomize` approach):
  what makes self-mutation and or3's subprocess-heavy tests measurable. The old
  `mutation.yml` workflow goes with 1.0 and returns with this.
- **Stryker report schema**: a second JSON shape for dashboards; add when wanted.

## Stages

Each stage ends green, committed on the branch, with its tests. The user reviews after
stage 1 and reviews the benchmark harness before its numbers are believed; stages 2 and
3 may run through on their own tests.

1. **Engine end to end on one fixture project.** The finder, instrumentation with
   `fn_value` only, the pristine parent, child 0 with the tracer, fork per mutant, the
   verdict files, the forced-fail canary, a console report. The first commit clears the
   old tree (`src/`, `tests/`, `bench/`, the engine docs, and the two old plans in
   `docs/plans/`, both superseded by this one); `docs/documentation.md`, this plan and
   its companion stay. Done when a hand-checked fixture project produces the verdicts
   its author knows to be true and the canary fails as it should.
2. **Correctness envelope.** The slow path; the three-arm verdict-equality harness
   (fork path, subprocess path, and ground truth: the mutant applied textually to a copy
   of the file and plain pytest run on it, independent of all of invective's machinery);
   budgets, timeouts and their confirmation; the reached flag; every refusal; fixture
   projects that hold the known traps: fixtures of every scope, parametrisation, async
   tests, a hanging mutant, a mutant reached at import, a test that prints and one that
   closes its stdout, a conftest that imports a target, a test that starts a thread, a
   test that starts an interpreter.

   Carried from stage 1's review:

   - `--basetemp` with more than one worker makes sibling children delete each other's
     directories; each child needs a basetemp of its own, as xdist gives each worker.
   - Each child's pytest temporary directory and its `.lock` are left behind, since
     `os._exit` skips the atexit handlers that remove them.
   - A child's wall budget leaves out the setup of a shared fixture that a non-covering
     test paid for in the baseline (budgets are this stage's).
   - The `fn_value` replacements name `iter`, `set` and `frozenset` as globals, which a
     target that defines a module-level `set` would shadow.
   - pytest's `--import-mode=importlib` imports a package `__init__.py` past the finder.
   - Files the parent opened (`--log-file`, `--report-log`, faulthandler's descriptor)
     are inherited by every child.
   - A kill caused by a test's own order dependence on a non-covering test is counted
     as a kill; the verdict-equality harness is where it shows.

3. **Operators and scope.** The binary tables above, structural ids, trivial
   equivalence if it stays trivial, accept comments with stale detection,
   `--changed-since`, the JSON report, exit codes, `--fail-fast`, kill-history ordering,
   `[tool.invective]`, the `invective run` wrapper.
4. **Measurement and release.** The benchmark harness and its run; the Finding; the
   README and docs through the write-doc skill (its examples must cite the new engine's
   symbols, not the old tree's); version 1.0.0 and `uv lock`; the `checks` workflow for
   3.12 to 3.14; the pull request against `main`, not merged.

## Testing

One complete suite, no tiers. Its shape is real pytest runs on small fixture projects,
because every failure mode that matters is a process-level one, and its backbone is the
three-arm equality harness from stage 2, run on every fixture project. A new or changed
check must be seen red before its green is trusted. invective's own suite may run on
xdist workers; the engine's children never do.

## Benchmark

Three pure-Python, pytest-based, widely used projects, pinned by version: `attrs`
(small, heavily tested, the classic subject, gremlins' own gate), `more-itertools`
(pure functions and hot loops, which expose the switch's cost), `click` (fixtures and a
CLI test runner, closer to an application). Each gets a whole-package run and a
branch-sized run of two or three modules.

Four arms: the new engine, invective 0.5.1, the gremlins fork at the commit or3 pins, and
mutmut 3 (3.8 or newer). Each arm in its own virtual environment per project, never
shared, since the old and new invective plugins share a name and every tool registers
pytest plugins. Arms run interleaved, turn about, several passes, as or3's
testing-performance page prescribes; a comparison that was not interleaved is not
evidence. Report wall time, mutant counts (the operator sets differ, and a count beside a
time keeps the claim honest), and verdict agreement on the operators whose tables are
shared.

A short benchmark runs before the full one: one project, the branch-sized case, one
pass. Its numbers are read with the user, who decides whether performance is chased
before the full run. The full run has no cap on an arm's time.

**The bar for 1.0**: at least as fast as every other arm on every case all arms
complete, the smallest case included, since that is where fixed cost shows. mutmut 3 is
the arm that matters; it forks too. If a tool cannot complete a case, that is recorded
and the case is judged on the arms that can.

## Repository mechanics

The work lives on the branch this session was given, reaches `main` through one pull
request, and is not merged by the implementers. Version becomes 1.0.0 in the same pull
request, with `uv lock` regenerated. Copied tables carry the fork's MIT notice. The two
old plans are deleted in stage 1. Commit messages say what was wrong and what is true
now, never the file list.
