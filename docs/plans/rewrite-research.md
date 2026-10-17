# Rewrite research

The measurements and reading behind `docs/plans/rewrite.md`: optional context for an
agent questioning one of its decisions; deleted with the plan.

## What is here

Each section is independent. Read the one that bears on the decision in front of you.
Nothing here is required to build.

## The pytest-gremlins tracker

All 335 issues of `mikelane/pytest-gremlins` were read on 2026-10-17, through seven
agents, each summarising a slice. What bears on the execution model:

- **Cost.** The maintainer's profile (#49) put the subprocess wait at about 90% of
  sequential time, 500 to 700 ms per spawn, with switching and import hooks under 1%.
  #538 measured about 900 ms of pytest startup per bootstrap; a runner that skipped it
  ran 64 mutants in 58 s against 317 s. The #348 spike measured per-mutation cost at
  29.8 ms subprocess, 1.34 ms fork, 0.11 ms hybrid, 0.003 ms in-process.
- **Fork and in-process never ran** (#532): the executors took tests from a command line
  with no node ids, so every mutant ran zero tests, and they toggled the switch in a
  process that had never installed the import hook. Unit tests passed because they
  hand-built both inputs. The companion "lightweight runner" fabricated verdicts by
  calling test functions outside pytest (#486, #492: 321 of 321 spurious kills). The
  maintainer's redesign list (#532, #538) names fork-after-collect from a collected
  parent as a candidate.
- **Parent-child drift** is the largest bug class for a subprocess per mutant: `--cov`,
  `addopts`, `-n`, `-p`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD`, rerun settings and ini values
  failing to reach, or wrongly reaching, the child (#113, #129, #340, #502, #546, #654,
  #656, #658, #668, #669, #670, #679). Mutants that broke import or collection cost 147
  of 681 ERRORs on attrs and 451 s of confirmation runs (#550, #553). A forked child
  inherits the exact plugin set and configuration.
- **Import-time code** is the one hole in a warmed parent (#348, #346, #91): about 15%
  of gremlins' mutants sat in module-level code that runs before any toggle. Function
  bodies only, as cargo-mutants scopes them, empties that class; functions called at
  import time remain and take the slow path.
- **The switch must be a plain global** (#346): a per-call environment lookup made
  more-itertools 68% slower (8:38 to 14:33).
- **Timeouts** are a kill only after a solo re-run and an unmutated control run
  (#565): contention gave a fake 42.9% against 0% sequential.
- **Verdicts as data, never a bare exit code** (#93, #484, #656): a startup crash was
  scored as a kill more than once.
- **Instrumented code** must be served under its real path with `__file__` set, keyed
  by resolved origin, ahead of pytest's assertion-rewrite hook (#525, #563, #591, #597,
  #603).
- **Coverage.py's sysmon core** drops per-test contexts on 3.14 (#531); the pre-scan was
  91% of the pre-run time in the profiling report (#503, #527).
- **In-process state leaks** between phases in one process (#376, #559).
- **Diagnostics go to stderr**, never `warnings.warn`: a project with
  `filterwarnings = error` turns the tool's own warning into a failure (#543).
- **Captured callables** (#416): patching a module-level name misses a function captured
  at import time (a dataclass default factory). A switch inside the function body is
  immune, since the captured object is the instrumented one.

## Startup cost on a real suite

> **Finding:** (2026-10-17, this container, 4 vCPU, Python 3.13.16, or3 at `0932c37`,
> warm page cache, `-n 0`)
>
> `import pytest`: 0.15 s. `pytest --collect-only` of one small test file: 0.38 s. Of
> the whole suite (4,836 tests): 2.1 s warm, 9.9 s cold. A subprocess per mutant pays
> at least the first two of those per mutant; a fork pays about a millisecond.

## Pooling several mutants per child

The question was whether fixture setup repeated per child is worth sharing by running
several mutants with disjoint covering tests in one child.

> **Finding:** (2026-10-17, or3 at `0932c37`, 4 vCPU Xeon 2.10 GHz, CPython 3.13.16,
> pytest 9.1.1, coverage 7.16.2 with CTracer, the gremlins fork's `fn_value` and
> `binary_operator` operators; two serial runs, one for timing and one for per-test
> coverage; every mutant assumed to run all of its covering tests)
>
> Suite wall time 141.7 s. Higher-scope fixture setup in the whole suite: 1.11 s,
> 0.78% of wall. 8,060 mutants over all of or3's source, 591 (7.3%) unreached; covering
> set size median 14, p90 121, max 4,834. Fixture setup as a share of solo cost:
> 0.245%. Greedy disjoint pooling saving: 0.082%; the ceiling for any packing 0.24%.
> Over three recent branches' changed modules: 0.008%, 0.080%, 0.112%. The expensive
> work (venvs, second interpreters, servers) sits in test bodies and function-scoped
> fixtures, which pooling cannot share. Pooling is also unsound across mutants that
> share memoised state: a cache filled by mutant A's test serves mutant B's test a value
> without executing A's site, which the per-test reach bits cannot see.

## mutmut 3

Read from the `boxed/mutmut` main branch (3.8.0) on 2026-10-17: the fork and fork-server
runners in its `workers` package, the pytest harness in `runners`, the trampoline and the
mutators in `mutation`, and its architecture document.

| | mutmut 3.8 | invective 1.0 plan |
|---|---|---|
| after fork | `pytest.main(["-x", ...tests])`: a new session and a collection per mutant | already-collected items through `pytest_runtest_protocol` |
| the parent | default `fork`: has run the whole suite; `forkserver`: clean, imported, optionally collected | collected, never ran a test |
| the switch | per-call wrapper: `os.environ` lookup, `rpartition`, module compare, dict lookup, argument repacking | one module-global compare |
| selection | functions each test's trampolines hit (function level); optional coverage.py line filter | per-test line map from `sys.monitoring` |
| operators | broad: numbers +1, strings, argument removal, `None` assignments, keywords, one swap per operator | two genres with minimal tables |
| verdict | exit code; timeout counted as killed; wall limit 15 × estimate + 1 s | verdict file; timeout confirmed |
| mutants live | a `mutants/` copy on disk, cwd changed into it | in memory via the finder |

Taken from it: the forced-fail canary, `gc.freeze()` before forking, `RLIMIT_CPU`
alongside the wall budget, fork-server restarts for fork-unsafe setups (later),
`max_stack_depth` as an ordering idea (later).

## Operators and the literature

- **Selective mutation** has three generations. Omitting prolific operators (Offutt,
  Rothermel, Untch, ICSE 1993; about 60% fewer mutants, scores above 99%). Sufficient
  sets (Offutt et al., TOSEM 1996: five operators, about 77% fewer mutants, 99.5%
  score; Wong and Mathur, JSS 1995; Namin, Andrews and Murdoch, ICSE 2008). The humbling
  result: for estimating a score, operator selection does no better than random
  sampling (Zhang et al., ICSE 2010; Gopinath et al., ICSE 2016). Statement deletion
  alone carries most of the signal (Deng, Offutt, Li, ICST 2013; Delamaro, Offutt,
  Ammann, ICST 2014); `fn_value` is its function-level member.
- **Subsumption within an operator** is the reduction that loses nothing for a per-line
  verdict: three of seven relational replacements suffice for all inputs (Kaminski,
  Ammann, Offutt, AST 2011 and JSS 2013), and the same holds for conditional and unary
  operators (Just, Kapfhammer, Schweiggert, ISSRE 2012). The tables in the plan are
  theirs. cargo-mutants' table for `<` is `<=`, `>`, `==`: the same count and weaker,
  since it never demands a test in the `a > b` region.
- **Infection** (weak mutation: Howden 1982; Offutt and Lee 1994; Just, Ernst, Holmes,
  ISSTA 2014, about 40% less analysis time): a mutant that never changes a value under
  any covering test cannot be killed by them. The probe must evaluate alternatives only
  on an allowlist of builtin types, treat an exception as infection, treat a
  short-circuited boolean as infected, and leave `fn_value` out (a replaced body loses
  side effects the probe cannot see).
- **Split-stream execution** (MuVM, ICST 2016; AccMut, ISSTA 2017; WinMut 2021): fork at
  the first mutation site inside a test. Fragile because every child shares the test's
  open files, sockets, temp directories and subprocesses from the fork point on, sites
  are reached many times, and teardown is shared.
- **Second-order mutation and JudyDiffOp** (Madeyski, Orzeszyna, Torkar, Józala, TSE
  2014): pairing first-order mutants from different operators cut mutants by 63.5% and
  equivalents by more, at the cost of inflated scores; it answers "is this line tested"
  for neither line of a pair, so it is not for a gate.
- **Mutation testing as a gate**: Google (ICSE-SEIP 2018; TSE 2021) and Facebook
  (ICSE-SEIP 2021) surface diff-scoped mutants in review and do not block or report a
  score; cargo-mutants ships `--in-diff` for CI. The design keeps the gate semantics as
  a thin layer because the speed levers are reliability-neutral: nothing in the engine
  changes if a project turns the gate into an advisory report.
