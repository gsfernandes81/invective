# Benchmarking

How a speedup is measured: `bench/compare.py`, the cases it runs, how to read
what it writes, and what a pull request's measurement includes; procedure.

## One command

```console
uv run bench/compare.py main HEAD --case R40 --case M60 --workers 1,4 --pairs 3
```

The two positional arguments are git refs of this repository: the side
measured against (before) and the side measured (after). The tool needs git
and uv and nothing else: `uv run` reads the script's own metadata, so it runs
without this project's environment, and uv downloads the Python the venvs
ask for when the machine lacks it. The first session fetches each project a
case needs, by full commit, into a cache outside the repository; later
sessions reuse it.

Before any heavy work it prints one line of advice: the performance power
profile, AC power, and nothing else heavy running. A machine that drifts
anyway shows it as discarded pairs.

A sanity check of the whole chain takes one pair of the first case at the
first count of workers, without repeats:

```console
uv run bench/compare.py main HEAD --quick
```

A feature compared with itself turned off is the same ref twice, with the
setting on one side:

```console
uv run bench/compare.py HEAD HEAD --case R40 --workers 4 --set-after confirm=true
```

A feature that remembers (history, a cache) is measured warm, each side's
timed run following an untimed one; a cache's re-run is also held to the
pytest processes it must start:

```console
uv run bench/compare.py main HEAD --case R40 --workers 1,4 --unit warm --expect-rerun-processes
```

The recorded R40 run at four workers with `--confirm` follows the pairs:

```console
uv run bench/compare.py main HEAD --case R40 --workers 1,4 --confirm-run
```

| Flag | What it sets |
|---|---|
| `--case NAME` | a case of `bench/cases.toml`; repeated or comma-separated; R40 when none is given |
| `--workers 1,4` | the counts of workers, each a block of its own; 1 when not given |
| `--pairs N` | the pairs kept for each case and count; 3 when not given |
| `--repeats N` | the most pairs repeated after a discard, per case and count; 2 when not given |
| `--quick` | one pair of the first case at the first count, no repeat |
| `--python V` | the Python of both venvs, as uv takes it; 3.14 when not given |
| `--set K=V` | a `[tool.invective]` setting in both sides' copies of the project |
| `--set-before K=V`, `--set-after K=V` | the same, on one side |
| `--env K=V` | an environment variable on both sides |
| `--env-before K=V`, `--env-after K=V` | the same, on one side |
| `--with PACKAGE` | another package installed in both venvs |
| `--fixed CLASSES` | what the touchable part leaves out (below); `baseline,timeout` when not given |
| `--threshold X` | the discard threshold, in place of 1.15 and its adjustment |
| `--timeout SECONDS` | a run past this is ended and recorded as a timeout |
| `--out DIR` | where the results go; when not given, a new directory named for the time, under `results` in `bench/` |
| `--unit cold`, `--unit warm` | each slot of a pair one timed run, or an untimed run then the timed one (below); cold when not given |
| `--same-processes` | exit 4 unless the two sides of every pair started as many pytest processes |
| `--expect-rerun-processes` | with `--unit warm`: exit 4 unless each timed run after starts its untimed run's runs of the original and one run a kill by time |
| `--confirm-run` | after the pairs, one recorded run of each case by the after side at the largest count of workers, with `--confirm`; exit 5 unless it names nothing it could not reproduce |
| `--setup-only` | set up and check every project, then stop before the first pair |
| `--keep` | keep the worktrees, venvs and copies, to look at a run that failed |

`--set` writes the setting into the project's `pyproject.toml` in that side's
copy, so the value is TOML (`true`, `4`); a value that is not TOML is taken as
a string. A `PYTEST_ADDOPTS` given with `--env` is added after the project's
deselections rather than replacing them.

## What it controls for

- **Both sides alike.** Each side gets a git worktree of its ref and a uv venv
  of the same Python, holding invective from that worktree, pytest, and what the
  cases' projects require, with every module compiled when it is installed. Each
  project is copied once for each side, so the two never share a tree. Every
  slot of a pair starts from nothing remembered: the copy's `.invective` is
  removed before it, whether or not the engine keeps anything there. Both sides
  are timed by
  the same instrument, the tool's own (`bench/instrument.py`), whichever ref it
  measures. At one worker no `--workers` flag is passed, so a ref older than the
  workers can be the before side.
- **Order.** A pair is one slot of each side, of one case at one count of
  workers, back to back. Pairs alternate their order: before then after, then
  after then before, and so on, so a drift in one direction favours neither side.
  The pairs of a case and count run one after another, in a block.
- **Units.** Cold (the default), a slot is one timed run. Warm (`--unit warm`),
  a slot is an untimed run and then the timed one, which has the untimed run's
  `.invective` to remember, so the timed run is what a user's second run costs:
  a warm history, or a cache's unchanged re-run. Only the timed run is compared,
  and the untimed run's wall time, status, verdict counts and pytest processes
  are recorded beside it. Its verdicts are held to the case's as well, since a
  run that remembers must come to what the one it remembers did. An untimed run
  that fails ends its slot with no timed run.
- **Drift.** A calibration (a plain serial `pytest -q -p no:cacheprovider` of one
  fixed file, in the before venv) runs just before and just after every slot, and
  back to back runs share the one between them. Calibrations are compared only
  with each other: a pair is discarded when the largest calibration around its
  runs is more than 1.15x the smallest (`compare.THRESHOLD` in the tool). When a
  calibration of the session's first pair spreads more than 5% on its own runs,
  the threshold for the session is 1 + three times that spread, and the summary
  says so. Each calibration command runs once, untimed, before the first pair:
  its first run writes the tests' compiled files, and a project that is not
  green stops the session there.
- **Repeats.** A discarded pair, or one whose run died, is run again, at most
  twice in a block. A block that still has fewer kept pairs than asked is
  inconclusive. A refusal (exit 2) ends its block at once, since it comes back
  every time.
- **The noise floor `f`.** Alternating pairs put two runs of one side next to
  each other at every boundary between pairs. The ratio of each such pair of
  neighbours is a measure of the noise, and `f` is the mean of `|r - 1|` over
  every neighbour on one project at one count of workers, its cases pooled. With
  fewer than three neighbours `f` is at least 2%; with none, it is the largest
  floor in the session, and at least 2%. A speedup within `2f` of 1 is no change.
- **A test that times the machine.** More-itertools'
  `TestConcurrentTee::test_concurrent_consumers`, in its `test_more.py`, takes
  1.4 to 96 s while the other CPUs are idle and a steady 1.5 s while they are
  busy, so every run on that project deselects it (the project's `deselect` in
  the cases file) through `PYTEST_ADDOPTS`, which reaches every pytest invective
  starts. It is not passed among the tests, which would make the selection not
  plain. Before the first pair the tool collects that file with the deselection
  and stops if the test is still there.
- **The verdicts.** Every run's report is compared with the first run of its
  case, whichever side and count of workers that was, mutant by mutant: target,
  kind, line and change. A kill is a kill whichever test made it. A difference
  where one side's verdict is a kill by time or a run a signal ended is put down
  to load: it is printed and recorded and does not fail the session. Any other
  difference is printed as it is found, written to `verdicts.txt`, and makes the
  exit status 3.
- **The touchable part.** The instrument wraps `mutate.Copy.run` and records
  every run a campaign makes: its mutant, its selection, its seconds and what it
  came to. At one worker the tool subtracts, on each side, the seconds of the
  runs a change cannot touch, matched by mutant from the before side's report:
  `baseline`, each run of the original on the campaign's own selection (a gate
  or a probe runs a file of its own and stays touchable); `timeout`, the mutants
  the before side killed by time; `survivor`, the ones it let through. The
  touchable speedup is the ratio of what is left, and the ceiling is the
  whole-run speedup if the touchable part cost nothing. With several workers the
  runs overlap, their seconds are no part of the wall time, and only the whole
  run is reported.
- **The pytest processes.** Each row the instrument records is one
  `mutate.Copy.run`, which starts one pytest process, so a run's rows are the
  count of processes it started. It needs no timing, so it decides before any
  ratio does. Every pair's two counts are recorded and reported.
  `--same-processes` fails the session (exit 4) on a pair whose sides differ, or
  where a side has no count: a cold run of a feature with nothing remembered
  starts what the engine before it started. `--expect-rerun-processes` holds
  each timed run of the after side, in a warm unit, to the count a re-run that
  remembers every verdict it can starts: the untimed run's runs of the original
  on the campaign's own selection (every copy's baseline, and the first copy's
  second run of the original that several workers make), and one run for each
  mutant the untimed run killed by time, since a kill by time is never
  remembered. A gate or a probe runs a file of its own and is not counted among
  them.
- **The confirm run.** `--confirm-run` adds, after the pairs, one cold run of
  each case by the after side at the largest count of workers with `--confirm`.
  It is recorded, never judged on time, and its report's `unreproduced` must be
  empty: a kill it names, or a report that does not say (an engine without
  confirmation, a run that died), fails the session with exit 5. One worker
  confirms nothing, so it needs a count above 1 in `--workers`.

Each run's wall time is taken around the process on every platform; its user
and system seconds come from `resource`, which Windows lacks. A ^C or a SIGTERM
ends the run in progress (a ^C at the terminal reaches it too, a SIGTERM is
passed on to it), records it as stopped, writes the summary of what ran, and
removes the worktrees, venvs and copies.

## Reading the output

The out directory holds:

| File | What it holds |
|---|---|
| `summary.txt` | the sides, the machine, the table below, every discard, died run and inconclusive case, every process count not met, and the confirm run's `unreproduced` |
| `finding.md` | a `**Finding:**` block in `CLAUDE.md`'s form, ready to paste |
| `runs.tsv` | one row a slot: wall, user, sys, the calibrations before and after it, exit code, status, verdict counts, pytest processes (`runs`), fixed seconds, phase marks, the unit; for a warm slot the untimed run's (`prime_`) wall, exit, status, verdict counts and processes; the expected processes of a re-run; and for the confirm run (pair `confirm`) how many kills it could not reproduce |
| `pairs.tsv` | one row a pair: its order, calibrations, spread, status, ratios, and each side's pytest processes with whether they agree and what a re-run was expected to start |
| `calibrations.tsv` | one row a calibration: its runs and their median |
| `verdicts.txt` | every verdict difference, when there is one |
| `session.json` | the command line, the conditions and each block's pair statuses |
| `venv-before.txt`, `venv-after.txt` | each venv's Python and installed packages |
| `logs/` | each run's output, report, instrument rows and marks, and each calibration's output |

A run's status is `ok`, `refused` (exit 2), `died` (any other exit, or no
report), `timeout` or `stopped`. The marks are the seconds from the start at
which the campaign said its `baseline:` line, closed its pool, first restored a
copy for `--confirm`, and returned its report.

The summary's columns, one row for each case and count of workers:

| Column | What it is |
|---|---|
| `pairs` | kept of asked, and in brackets how many were discarded or died |
| `before`, `after` | the median wall time of each side's runs in kept pairs |
| `speedup` | before over after, so above 1 is faster: the median over kept pairs of the ratio within each pair, not a ratio of medians |
| `range` | the lowest and highest of those ratios |
| `touchable`, `ceiling` | the same median for the touchable part, and the ceiling, at one worker |
| `f (nbrs)` | the noise floor and how many neighbours it came from |
| `beyond 2f` | whether the speedup is further than `2f` from 1, or `inconclusive` |
| `processes` | the median pytest processes before and after, `differ` when a kept pair's two differed, and `rerun ok` or `rerun missed` under `--expect-rerun-processes` |
| `verdicts` | `equal`, `load only`, or `DIFFER` |

| Exit status | Meaning |
|---|---|
| 0 | every case concluded, with equal verdicts |
| 1 | a run died, or a case is inconclusive |
| 2 | the setup or a calibration failed, or the arguments are wrong |
| 3 | the verdicts differ |
| 4 | a pytest process count asked for was not met |
| 5 | the confirm run named a kill it could not reproduce, or did not say |
| 130 | stopped by a ^C or a SIGTERM |

The Finding's closing sentences state the numbers. Before it goes into a page,
they are edited to say what the numbers show for the change measured.

## Adding a case

A case is one table in `bench/cases.toml`, and a project another, when the
case needs a project the file does not have:

```toml
[cases.R10]
project = "more-itertools"
target = "more_itertools/recipes.py"
tests = ["tests/test_recipes.py"]
only = "CMP,RAISE"
limit = 10
```

An unknown key in either is an error, not a key ignored.

| Case key | What it is |
|---|---|
| `project` | the project it runs on |
| `target` | the module mutated, from the project's top |
| `tests` | the tests, as plain paths, so the selection stays plain |
| `collect`, `collect_exclude` | in place of `tests`: every file `pytest --collect-only -q` names under these paths, less those whose path holds an excluded string |
| `only`, `limit` | the kinds of edit and the cap, as `invective run` takes them |
| `via` | `run` for `invective run`, `pytest` for the plugin's `--mutate` |
| `options` | arguments before the plugin's own, such as `-n 0` |

| Project key | What it is |
|---|---|
| `repo`, `commit` | where it is fetched from, and its full commit |
| `path` | a directory to copy instead, from the cases file's directory |
| `self` | `true`: invective's own worktree of each side |
| `requires` | packages its tests need, installed in both venvs |
| `pyproject` | lines put at the top of tables of its `pyproject.toml`, by table name |
| `deselect` | node ids deselected in every run, through `PYTEST_ADDOPTS` |
| `calibrate` | the calibration's test paths; `"tests"` for the case's own; empty for none |
| `calibrate_runs` | the runs a calibration takes the median of; `"auto"` for one past 20 s, else three |

A project with no calibration has no pair discarded. That is right only when a
run is too short for a ratio of calibrations to mean anything (invective's own,
under 3 s), and such a case is recorded, not judged on time.

## What a pull request's measurement includes

- **The sides.** Before is `main` at the pull request's base, after is its
  head; or the head twice, with its feature off before and on after.
- **The conditions.** Python 3.14, `confirm` off in every judged run (its
  default), the unit the rule judges (cold, or warm for a feature that
  remembers), and the cases and counts of workers the pull request's plan
  judges, three pairs each. R40 at one and at four workers is in every
  measurement, since it is the case the noise floor of more-itertools comes from.
- **The proxies.** Where the plan has a process count, it is checked before any
  time: `--same-processes` on cold runs of a feature with nothing remembered,
  `--expect-rerun-processes` on a cache's warm re-runs.
- **The confirm run.** `--confirm-run` with four among the counts of workers,
  and its `unreproduced` empty, or explained in the Finding.
- **The numbers.** The whole-run speedup first; for a speedup rule, the
  touchable speedup and the ceiling beside it, with `--fixed` naming what the
  change cannot touch. Every number with its floor `f` and the count of
  neighbours behind it: a threshold is never below `2f`.
- **The verdicts.** Equal between the sides and across counts of workers. Any
  difference blocks the merge until it is explained, except one made only of
  kills by time or by a signal, which is recorded.
- **What was dropped.** Every discarded pair, died run and inconclusive case,
  from the summary.
- **The Finding.** The block from `finding.md`, its closing sentences edited to
  the conclusion, in the page the feature belongs to. The pull request's
  description carries the summary's table.

## The tool against itself

> **Finding:** (2026-10-10, a 4-vCPU Firecracker VM (Intel Xeon at 2.80 GHz) with nothing
> else running, Linux 6.18, Python 3.14.6; before and after both `main` at `f989e8f`;
> R40 is `invective run --target more_itertools/recipes.py --tests tests/test_recipes.py
> --only CMP,BOOL,NOT,CONST,RAISE --limit 40` on more-itertools at `81c21a8`, with
> `TestConcurrentTee::test_concurrent_consumers` deselected; `--quick`, so one pair at
> one worker, before then after; calibration the median of three plain runs of
> `test_recipes.py`)
>
> | side | wall | calibration before | calibration after | killed | survived |
> |---|---|---|---|---|---|
> | before | 292.6 s | 8.74 s | 8.84 s | 28 (4 by time) | 12 |
> | after | 292.9 s | 8.84 s | 8.78 s | 28 (4 by time) | 12 |
>
> The pair's speedup was 0.999x on the whole run and 0.998x on the touchable part (129 s
> of each run was the baseline and the kills by time, a ceiling of 2.27x), and the
> verdicts were identical. One calibration's own three runs spread 6.7%, so the session's
> threshold rose to 1.20x; the pair's calibrations spread 1.1%, and it was kept. Two
> sides of one commit measure as the same, to a tenth of a percent.
