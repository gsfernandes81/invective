# Benchmarking

How a speedup is measured: `bench/compare.py`, the cases it runs, how to read
what it writes, and what a pull request's measurement includes; procedure.

## One command

```console
uv run bench/compare.py origin/main HEAD --case R40 --workers 1,4 --pairs 3
```

The two positional arguments are git refs of this repository: the side
measured against (before) and the side measured (after). They are taken as
the repository has them, so a local `main` that is behind is the before side
as it stands; `origin/main`, after a `git fetch`, is the base a pull request
is merged into. The tool prints the commit each ref resolved to.

The tool needs git and uv and nothing else: `uv run` reads the script's own
metadata, so it runs without this project's environment, and uv downloads the
Python the venvs ask for when the machine lacks it. Before any heavy work it
prints one line of advice: the performance power profile, AC power, and
nothing else heavy running. A machine that drifts anyway shows it as
discarded pairs.

A sanity check of the whole chain takes one pair of the first case at the
first count of workers, without repeats:

```console
uv run bench/compare.py origin/main HEAD --quick
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
uv run bench/compare.py origin/main HEAD --case R40 --workers 1,4 --unit warm --expect-rerun-processes
```

The recorded run with `--confirm` follows the pairs, one for each case given,
at the largest count of workers:

```console
uv run bench/compare.py origin/main HEAD --case R40 --workers 1,4 --confirm-run
```

| Flag | What it sets |
|---|---|
| `--case NAME` | a case of the cases file; repeated or comma-separated; R40 when none is given |
| `--workers 1,4` | the counts of workers, each a block of its own; 1 when not given |
| `--pairs N` | the pairs kept for each case and count; 3 when not given |
| `--repeats N` | the most pairs repeated after a discard or a death, per case and count; 2 when not given |
| `--quick` | one pair of the first case at the first count, no repeat |
| `--python V` | the Python of both venvs, as uv takes it; 3.14 when not given |
| `--set K=V` | a `[tool.invective]` setting in both sides' copies of the project |
| `--set-before K=V`, `--set-after K=V` | the same, on one side |
| `--env K=V` | an environment variable on both sides |
| `--env-before K=V`, `--env-after K=V` | the same, on one side |
| `--with PACKAGE` | another package installed in both venvs |
| `--fixed CLASSES` | what the touchable part leaves out (below); baseline,timeout when not given |
| `--threshold X` | the discard threshold, in place of the default and its relaxation |
| `--timeout SECONDS` | a run past this is ended and recorded as a timeout |
| `--unit cold`, `--unit warm` | each slot of a pair one timed run, or an untimed run then the timed one (below); cold when not given |
| `--same-processes` | exit 4 unless the two sides of every pair started as many pytest processes |
| `--expect-rerun-processes` | with `--unit warm`: exit 4 unless each timed run after starts what a re-run is expected to (below) |
| `--confirm-run` | after the pairs, one recorded run of each case by the after side at the largest count of workers, with `--confirm`; exit 5 unless it names nothing it could not reproduce |
| `--out DIR` | where the results go; when not given, a new directory named for the time, under `results` in `bench/` |
| `--cases-file FILE` | the cases and projects; `bench/cases.toml` when not given |
| `--cache DIR` | where fetched projects are kept (below) |
| `--repo DIR` | the git repository the refs are read from and the worktrees made in; the one the tool is in when not given |
| `--work DIR` | where the worktrees, venvs and copies go; the system's temporary directory when not given |
| `--setup-only` | set up and check every project, then stop before the first pair |
| `--keep` | keep the worktrees, venvs and copies, to look at a run that failed |

`--set` writes the setting into the project's `pyproject.toml` in that side's
copy, so the value is TOML (`true`, `4`); a value that is not TOML is taken as
a string. A `PYTEST_ADDOPTS` given with `--env` is added after the project's
deselections rather than replacing them.

### The cache

Each project a case needs is fetched once, by its full commit, into the cache,
and copied from there for every session. When `--cache` is not given it is
`invective-bench` under `$XDG_CACHE_HOME` (else `~/.cache`) on Linux,
`~/Library/Caches` on macOS, and `%LOCALAPPDATA%` on Windows. Each project is
a bare repository holding one commit, named for the project; removing the
directory, or one project's repository in it, clears it, and the next session
fetches again.

### What a killed session leaves

Every session's work directory (`invective-bench-*` in `--work`) holds a file
naming the process it is for, and each session starts by removing those whose
process is gone and pruning the worktree entries whose directory is gone. A
directory without that file is named and left. By hand, `git worktree prune`
and removing the `invective-bench-*` directories while no session runs clear
it all.

## What it controls for

- **Both sides alike.** Each side gets a git worktree of its ref and a uv venv
  of the same Python, holding invective from that worktree, pytest, and what the
  cases' projects require, with every module compiled when it is installed. Each
  project is copied once for each side, so the two never share a tree, and once
  more for the tool's own pytests (below). Every slot of a pair starts from
  nothing remembered: the copy's `.invective` is removed before it, whether or
  not the engine keeps anything there. Both sides are timed by the same
  instrument, the tool's own (`bench/instrument.py`), whichever ref it measures.
  At one worker no `--workers` flag is passed, so a ref older than the workers
  can be the before side.
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
  that fails ends its slot with no timed run. Both sides run both, though the
  before side's engine may remember nothing.
- **Drift.** A calibration (a plain serial `pytest -q -p no:cacheprovider` of one
  fixed file, in the before venv) runs just before and just after every slot, and
  back to back slots share the one between them. Calibrations are compared only
  with each other: a pair is discarded when the largest calibration around its
  runs is more than 1.15x (`compare.THRESHOLD`) the smallest. When a calibration
  of the session's first pair spreads more than 5% (`compare.FIRST_PAIR_SPREAD`)
  on its own runs, the threshold for the session is 1 + three times that spread,
  at most 1.5x (`compare.THRESHOLD_MOST`); a machine that needs more is too noisy
  to measure on, and the summary says so. Each calibration command runs once,
  untimed, before the first pair: its first run writes the tests' compiled
  files, and a project that is not green stops the session there.
- **A third copy.** The calibrations, the collections of a case's tests and that
  first run are the tool's own pytests, and they run in a third copy of each
  project, with the project's edits and neither side's settings. What they leave
  in a tree (a `.hypothesis` database, a `.coverage`) would otherwise be copied
  into one side's campaigns and not the other's.
- **Repeats.** A discarded pair, or one whose run died, is run again, at most
  twice (`--repeats`) in a block. A block that still has fewer kept pairs than
  asked is inconclusive. A refusal (invective's exit 2) ends its block at once,
  since it comes back every time.
- **The noise floor `f`.** Alternating pairs put two runs of one side next to
  each other at every boundary between pairs. The ratio of each such pair of
  neighbours is a measure of the noise, and `f` is the mean of `|r - 1|` over
  every neighbour of kept pairs on one project at one count of workers, its
  cases pooled. With fewer than 3 (`compare.NEIGHBOURS_LEAST`) neighbours `f` is
  at least 2% (`compare.FLOOR_LEAST`); with none, it is the largest floor in the
  session, and at least that. The floor is pooled within one session: a case
  measured in a session of its own has only its own neighbours, so a case with
  none (one pair) is measured in the same session as R40. A speedup within `2f`
  of 1 is no change.
- **A test that times the machine.** More-itertools'
  `TestConcurrentTee::test_concurrent_consumers`, in its `test_more.py`, takes
  1.4 to 96 s while the other CPUs are idle and a steady 1.5 s while they are
  busy, so every run on that project deselects it (the project's `deselect` in
  the cases file) through `PYTEST_ADDOPTS`, which reaches every pytest invective
  starts. It is not passed among the tests, which would make the selection not
  plain. Before the first pair the tool collects that file with the deselection
  and stops if the test is still there.
- **The verdicts.** Every run's report is compared with the first run of its
  case, whichever side and count of workers that was, mutant by mutant. A mutant
  is its target, kind, line and change, and a mark of the lines its diff takes
  out and puts in (`compare.mutant_mark`): two mutants of one kind and change on
  one line (two `and`s) differ by the column edited. A kill is a kill whichever
  test made it. A difference where both sides have the mutant and one side's
  verdict is a kill by time, or a run a signal or a crash ended, is put down to
  load: it is printed and recorded and does not fail the session. Any other
  difference, a mutant one side lacks among them, is printed as it is found,
  written to `verdicts.txt`, and makes the exit status 3.
- **The touchable part.** The instrument wraps `mutate.Copy.run` and records
  every run a campaign makes: its mutant and mark, its selection, its seconds
  and what it came to. At one worker the tool subtracts, on each side, the
  seconds of the runs a change cannot touch, matched by mutant from the before
  side's report: `baseline`, each run of the original on the campaign's own
  selection (`compare.is_baseline`: a gate or a probe runs a file of its own, and
  a coverage run carries the coverage handshake, so neither is one);
  `timeout`, the mutants the before side killed by time; `survivor`, the ones it
  let through. What is left of the before side's wall time W is U, its
  touchable seconds. The touchable speedup is the ratio of what is left on each
  side, and the ceiling is the whole-run speedup if the touchable part cost
  nothing. The touchable speedup has a floor of its own: the whole run moving by
  `2f` of W is the touchable part moving by `1 / (1 - 2f W / U)`, and a
  touchable speedup is beyond its floor when it is past that (or the slowdown
  `1 / (1 + 2f W / U)`). When `2f W` is all of U or more, no touchable speedup
  shows. With several workers the runs overlap, their seconds are no part of the
  wall time, and only the whole run is reported.
- **The pytest processes.** Each row the instrument records is one
  `mutate.Copy.run`, which starts one pytest process, so a run's rows are the
  count of processes it started. It needs no timing, so it decides before any
  ratio does. Every pair's two counts are recorded and reported.
  `--same-processes` fails the session (exit 4) on a pair whose sides differ, or
  where a side has no count: a cold run of a feature with nothing remembered
  starts what the engine before it started. `--expect-rerun-processes` holds
  each timed run of the after side, in a warm unit, to the count a re-run that
  reads every verdict the cache keeps starts (`compare.rerun_expected`): the
  untimed run's baselines; its coverage run, when it made one, since a re-run
  makes it again; and every run it made for a mutant whose kill the cache does
  not keep (by time, by a signal or a crash, with no killer, or with pytest's
  codes 3 to 5), its narrowed run as well as its whole one. The untimed run starts
  from an empty `.invective`, so it gated and probed nothing. A feature whose
  re-run starts another run again adds its own term to that function.
- **The confirm run.** `--confirm-run` adds, after the pairs, one cold run of
  each case given by the after side at the largest count of workers, with
  `--confirm`. It is recorded, never judged on time, and its report's
  `unreproduced` must be empty: a kill it names, or a report that does not say
  (an engine without confirmation, a run that died), fails the session with exit
  5. One worker confirms nothing, so it needs a count above 1 in `--workers`.

Each run's wall time is taken around the process on every platform; its user
and system seconds come from `resource`, which Windows lacks. A ^C, a SIGTERM
or a SIGHUP (one ignored on entry, under `nohup`, stays ignored) ends the run
in progress (a ^C at the terminal reaches it too, the others pass it a
SIGTERM), records it as stopped, writes the summary of what ran, and removes the
worktrees, venvs and copies. A run that does not end within 30 s
(`compare.GRACE`) is terminated, then killed; the pytest processes it started
are its own to stop, and one it leaves running shows in the calibration after
it.

## Reading the output

The out directory holds:

| File | What it holds |
|---|---|
| `summary.txt` | the sides, the machine, the table below, every discard, died run and inconclusive case, every process count not met, and the confirm run's `unreproduced` |
| `finding.md` | a `**Finding:**` block in `CLAUDE.md`'s form, ready to paste |
| `runs.tsv` | one row a slot: wall, user, sys, the calibrations before and after it, exit code, status, verdict counts, pytest processes (`runs`), fixed seconds, phase marks, the unit; for a warm slot the untimed run's (`prime_`) wall, exit, status, verdict counts and processes; the expected processes of a re-run; and for the confirm run (pair `confirm`) how many kills it could not reproduce |
| `pairs.tsv` | one row a pair: its order, calibrations, spread, status, ratios, the before side's fixed and touchable seconds, and each side's pytest processes with whether they agree and what a re-run was expected to start |
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
| `before`, `after` | the median wall time of each side's runs in kept pairs; `before` is W |
| `speedup` | before over after, so above 1 is faster: the median over kept pairs of the ratio within each pair, not a ratio of medians |
| `range` | the lowest and highest of those ratios |
| `touchable`, `ceiling` | the same median for the touchable part, and the ceiling, at one worker |
| `f (nbrs)` | the noise floor and how many neighbours it came from |
| `beyond 2f` | whether the whole-run speedup is further than `2f` from 1, or `inconclusive` |
| `U` | the median touchable seconds of the before side's runs |
| `touch. needs` | the touchable speedup its floor needs, `1 / (1 - 2f W / U)`, or `inside f` when none can show |
| `touch. beyond` | whether the touchable speedup is beyond its floor |
| `processes` | the median pytest processes before and after, `differ` when a kept pair's two differed, and `rerun ok` or `rerun missed` under `--expect-rerun-processes` |
| `verdicts` | `equal`, `load only`, or `DIFFER` |

The exit status is the first of these that applies (`compare.EXIT_STATUSES`):

| Exit status | Meaning |
|---|---|
| 0 | every case concluded, with equal verdicts |
| 1 | a run died or was refused, or a case is inconclusive |
| 2 | the setup or a calibration failed, or the arguments are wrong |
| 3 | the verdicts differ |
| 4 | a pytest process count asked for was not met |
| 5 | the confirm run named a kill it could not reproduce, or did not say |
| 70 | the tool itself failed: a bug, its traceback printed |
| 130 | stopped by a ^C, a SIGTERM or a SIGHUP |

The Finding's closing sentences state the numbers; a second table gives W, U
and what the touchable part's floor needs. Before it goes into a page, the
sentences are edited to say what the numbers show for the change measured.

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
| `calibrate_runs` | the runs a calibration takes the median of; `"auto"` for one run past 20 s (`compare.AUTO_ONE_RUN`), else three |

A project with no calibration has no pair discarded. That is right only when a
run is too short for a ratio of calibrations to mean anything (invective's own,
under 3 s), and such a case is recorded, not judged on time.

## What the tool does not do

- A case is one `invective run` or one `pytest --mutate`. `invective sweep`
  (S10), a run stopped with `timeout -s INT 60` and then resumed, a re-run after
  an unrelated edit, and a re-run at another count of workers than the cold run
  before it cannot be expressed.
- A warm `--limit 10` run after a full cold one needs a case of its own, and
  nothing checks that its ten mutants are among the cold run's.
- A warm unit's untimed runs are a cold run of both sides, but no cold ratio is
  computed from them; a cold ratio is a session of its own.
- One session has one floor for each project and count of workers; a case
  measured in another session does not share it.

## What a pull request's measurement includes

- **The sides.** Before is `origin/main` at the pull request's base, after is
  its head; or the head twice, with its feature off before and on after.
- **The conditions.** Python 3.14, `confirm` off in every timed run (its
  default), and the unit its rule needs (cold, or warm for a feature that
  remembers). R40 at one and at four workers, three pairs each, is in every
  measurement, since it is the case the noise floor of more-itertools comes
  from. `--workers` is 1 when not given, so `--workers 1,4` says so. Any other
  case, its count of pairs (an hour-long case may have one) and its rule are
  the pull request's to state.
- **The numbers.** The whole-run speedup first; for a rule on the touchable
  part, the touchable speedup, W, U, the ceiling and what the floor needs beside
  it, with `--fixed` naming what the change cannot touch. Every number with its
  floor `f` and the count of neighbours behind it: a threshold is never below
  `2f`.
- **The process counts.** Where a rule counts pytest processes, the count is
  checked before any time: `--same-processes` on cold runs of a feature with
  nothing remembered, `--expect-rerun-processes` on a cache's warm re-runs.
- **The verdicts.** Equal between the sides and across counts of workers. Any
  difference blocks the merge until it is explained, except one made only of
  kills by time or by a signal, which is recorded.
- **The confirm run.** `--confirm-run` with R40 and four among the counts of
  workers, and its `unreproduced` empty, or explained in the Finding.
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
> threshold rose to 1.20x; the pair's calibrations spread 1.1%, and it was kept. In this
> one pair the two sides of one commit differ by 0.1%; with no neighbours, `f` is the 2%
> it is at least, not a measured floor.
