# Memory

The `.invective/` directory: the killer history and the verdict cache; settled.

## The directory

`store.DIRECTORY` at the project's top (`config.project_root`, the directory a
run's copy is made of), under `--ref` too. It is made on the first write, with
a `.gitignore` reading `store.IGNORE`, so nothing in it is committed. A
`.gitignore` already there is left as it is, and two runs making the directory
at once are both fine. The copy leaves it out (`tree.SKIPPED`).

Only the main thread writes in it, and every file is written whole or not at
all (`store.write_json`): written beside its place, then put there with
`os.replace`, so a reader finds the old file or the new one. On Windows a file a
reader holds cannot be replaced, and the write is tried `store.RETRIES` times
more. A write that fails, or a directory that cannot be made, is said on a
`warning:` line and dropped: the directory only ever saves time, and losing it
loses no verdict. A temporary file a writer killed outright leaves is removed
by the next write once it is older than `store.STALE`, and in the verdict cache
by a run's first write in its campaign's directory.

## The history

`store.HISTORY`, a JSON object: for each target, as a path from the top with
`/` separators, each mutant's key and the node id of the test that last killed
it.

The key (`store.mutant_keys`) is what the mutant is: its kind, its change, its
node's text (`ast.unparse`, or `ast.dump` where that cannot write it), its
line's text, and how many sites before it, in the engine's order, have all four
the same. It holds no line number, so an edit elsewhere in the file keeps it. A
key that matches another mutant than the one it was made for costs a run of
one test, since the probe decides nothing it does not kill.

What a run changes, for the mutants it measured:

| outcome | the entry |
|---|---|
| a kill by a test (`store.storable`: a node id, not `TIMEOUT`) | that test |
| a kill by time, by a module that would not import, or by nothing named | removed |
| a survivor, accepted or not | removed |

A mutant the run did not measure (left out by `--only` or `--limit`, or not
reached before a stop) keeps its entry. A key no longer among the target's
mutants, every kind counted, is removed.

The history is read once a run's mutants are known, and written on the way
out, whatever ends the run after its baseline: a ^C or SIGTERM keeps what had
come to a verdict by then. A run refused before its mutants (a red baseline)
writes nothing. The write reads the file again and changes only the run's own
target, so a run on another module that saved before it keeps its changes.
Two runs that save at the same moment can lose one's changes, whichever
modules they are on, which costs time only. A run that changes nothing writes
nothing. A file that is not a history (not JSON,
or not that shape) is said on a `warning:` line, read as empty, and written
over by the next write.

`history = false` in `[tool.invective]`, or `--no-unsafe-speedups`, reads
nothing and writes nothing. With `--tests` holding an option on the command
line, the history is not tried but is still written, since what the whole
selection measured is still each mutant's killer.

## What the history saves

R40 is `invective run --target more_itertools/recipes.py --tests
tests/test_recipes.py --only CMP,BOOL,NOT,CONST,RAISE --limit 40` on more-itertools
at `81c21a8`. I20 is `pytest -n 0 -p no:cacheprovider --mutate
src/invective/accept.py --mutate-only CMP,BOOL,NOT,CONST,RAISE tests/test_accept.py`
on invective's own tree. A cold run starts with no `.invective`; a warm one follows
an untimed run that left it. The touchable part is the wall time less the runs the
history cannot change: the baseline and the kills by time, and for a warm run the
survivors too. `f` is the noise floor, and a speedup within `2f` of 1 is no change.
Each was measured with `bench/compare.py`, `--unit cold` and `--unit warm`, as
`docs/benchmarking.md` describes.

> **Finding:** (2026-10-10, a 4-vCPU Firecracker VM, Intel Xeon at 2.80 GHz, Linux
> 6.18, Python 3.14.6; before `main` at `dcd47a5`, the engine this branch was made
> from, after this branch at `56ca1ff`; every more-itertools run deselects its
> `TestConcurrentTee::test_concurrent_consumers`, a test that times the machine;
> three ABBA pairs per row, each kept, wall time, the median of the ratios within the
> pairs; a calibration of `pytest -q tests/test_recipes.py` around every run, a pair
> discarded past 1.15x cold and 1.21x warm; every `f` from two neighbours)
>
> | unit | case | N | before | after | speedup | touchable | f | processes |
> |---|---|---|---|---|---|---|---|---|
> | cold | R40 | 1 | 297.2 s | 300.1 s | 0.99x | 0.99x | 2.0% | 41 / 41 |
> | cold | R40 | 4 | 100.3 s | 97.5 s | 1.00x | none | 3.0% | 45 / 45 |
> | cold | I20 | 1 | 38.8 s | 39.0 s | 0.99x | 0.95x | 2.0% | 21 / 21 |
> | cold | I20 | 4 | 33.1 s | 33.0 s | 1.00x | none | 2.0% | 25 / 25 |
> | warm | R40 | 1 | 301.4 s | 273.2 s | 1.11x | 1.91x | 2.5% | 41 / 58 |
> | warm | R40 | 4 | 94.4 s | 93.9 s | 1.01x | none | 2.0% | 45 / 62 |
>
> | unit | case | N | W (before) | U (touchable) | touchable | its floor needs | ceiling |
> |---|---|---|---|---|---|---|---|
> | cold | R40 | 1 | 297.2 s | 168.3 s | 0.99x | 1.08x | 2.31x |
> | cold | I20 | 1 | 38.8 s | 8.0 s | 0.95x | 1.24x | 1.26x |
> | warm | R40 | 1 | 301.4 s | 65.7 s | 1.91x | 1.30x | 1.27x |
>
> Warm, at one worker, the remembered killer run alone roughly halves the time of the
> kills it reaches (1.91x on the touchable part, beyond the 1.30x its floor needs).
> The whole run gains 11% (1.07x to 1.12x across the pairs) against a ceiling of
> 1.27x: the baseline, the kills by time and the survivors are most of R40, and the
> history cannot touch them. Each gate, and each probe that does not kill, is one
> more pytest process of one test (58 against 41). At four workers the whole run does
> not change, warm or cold. A cold run costs nothing: no killer is remembered, so none
> is gated or tried, and both sides start the same pytest processes. The verdicts
> were identical in every run, and an R40 run at four workers with `--confirm` named
> no kill it could not reproduce.

> **Finding:** (2026-10-10, the owner's machine: AMD Ryzen AI 9 HX 370, 12 cores, 24
> logical CPUs, on AC, WSL2 with Linux 6.18, Python 3.14.7; before `main` at
> `dcd47a5`, after this branch at `56ca1ff`; R40 only, with the same deselection,
> pairs and calibration, a pair discarded past 1.15x cold; every `f` 2.0%)
>
> | unit | case | N | before | after | speedup | touchable | f | processes |
> |---|---|---|---|---|---|---|---|---|
> | cold | R40 | 1 | 209.9 s | 210.2 s | 1.00x | 1.00x | 2.0% | 41 / 41 |
> | cold | R40 | 4 | 63.1 s | 63.0 s | 1.00x | none | 2.0% | 45 / 45 |
> | warm | R40 | 1 | 207.6 s | 193.8 s | 1.07x | 1.92x | 2.0% | 41 / 58 |
> | warm | R40 | 4 | 62.7 s | 62.2 s | 1.01x | none | 2.0% | 45 / 62 |
>
> | unit | case | N | W (before) | U (touchable) | touchable | its floor needs | ceiling |
> |---|---|---|---|---|---|---|---|
> | warm | R40 | 1 | 207.6 s | 30.6 s | 1.92x | 1.37x | 1.17x |
>
> The same reading on a faster machine. Warm, at one worker, the kills the history
> reaches take about half the time (1.92x, beyond the 1.37x its floor needs), and the
> whole run gains 7% (1.07x to 1.08x) against a ceiling of 1.17x, the parts it cannot
> touch being a larger share of a shorter run. At four workers nothing changes, and a
> cold run costs nothing, with the same pytest processes on both sides. The verdicts
> were identical in every run.

## The verdict cache

With `cache = true` in `[tool.invective]` (off unless set), each verdict is kept as
it lands (`store.Cache`), and a later run of the same campaign reads it instead of
running the mutant (`mutate.cache_attempt`, the first attempt). There is no record of
a run as such: a run stopped any way at all resumes from what its verdicts left,
because each is one file written whole as it lands. The baseline is always run.

```text
.invective/cache/<target>/<campaign>/<mutant>.json
```

`<target>` is the first 12 hex digits of the SHA-256 of the target's path from the
top with `/`, `<campaign>` the first 24 of the campaign's key, and `<mutant>` the first
32 of the SHA-256 of the mutant's text. Two sites whose mutants are the same text are
the same program and share an entry.

### The campaign

`store.campaign_key` is the SHA-256 of canonical JSON of these:

| part | what it holds |
|---|---|
| `format` | `store.FORMAT` |
| `invective` | the installed version, and a hash of every `.py` file of `invective` and `pytest_invective` |
| `target` | the target's path, and a hash of its text in the first copy |
| `tests`, `options` | as the campaign runs them |
| `selected` | the tests the first baseline kept, in their order |
| `exclude` | `[tool.invective] exclude` |
| `files` | a hash of each file of the set below, by its path from the top |
| `env` | every `PYTHON*` and `PYTEST_*` variable but `PYTEST_CURRENT_TEST`, `PYTEST_VERSION` and `PYTEST_XDIST_*`, and `CI` |
| `python` | `sys.version`, `sys.platform`, the cache tag, `sys.prefix`, and the resolved `sys.executable` |
| `distributions` | each installed distribution's name, version, and the hashes of its `RECORD` and `direct_url.json` |

The files, with the top the first copy's, or under `--ref` the worktree's top
(`config.tree_top`):

1. for each selected test below the copy's top, every file under the top-level
   directory that holds it (`tests/`, or `src/` for a doctest); a selected file at the
   top, alone;
2. every `conftest.py` in the copy, and in each directory above it up to the top;
3. every pytest settings file in each directory from where pytest's search starts
   (`config._start`) up to the top, a `pyproject.toml` read as data, with
   `[tool.invective]` reduced to its `exclude` (`store._settings_digest`), even where
   rule 1 holds it too;
4. the `-c` file of the options;
5. every file the first baseline imported but the target (`INVECTIVE_INVENTORY`): one
   under the top by its path from it, any other whole. A module of a zip on `sys.path`
   is hashed as the archive, and one whose file is gone as absent.

The first copy is hashed whole before its first run (`store.hash_tree`), leaving out
what the copy leaves out and `tree.MARKER`, and so is each conftest and settings file
above it; the key is made once the baseline has said what it kept and imported, from
that, and a file the baseline imported that the copy did not hold (one it generated) is
hashed as it stands. A file that cannot be read, or a baseline that did not say what it
imported, leaves the cache unused for the run, said on the `cache:` line; it is never a
refusal.

Any file the baseline imported, a test or its data, a conftest, a setting pytest reads,
an installed package or the interpreter changed starts a fresh campaign, which reads
nothing kept before it. The other `[tool.invective]` settings are not in the key: the
workers are kept in each entry (`concurrent`), the switch too (`safe`), the history and
coverage as each kill's `origin`, `confirm` by the read rule below, and the rest change
no verdict. Neither are `--only` and `--limit`, so a run of a few mutants shares the
campaign of a run of all, nor the time budget: a kill kept ended inside its budget or
failed a test, and a survivor kept ended inside its own.

### What the key does not hold

A verdict can be read back after a change to any of these:

- a file a test or the code under test reads that nothing imports and the key does not
  hold: package data under `src/` read through `importlib.resources`, a schema in JSON,
  a file outside the test directories;
- a module only a mutant's run imports, what a child interpreter imports, or a `.pth`
  file;
- a distribution with no `RECORD`, and a file of an installed distribution edited in
  place, since its `RECORD` says what was installed;
- what decides a test by chance: random or replayed examples (Hypothesis and its
  `.hypothesis` database), and a flaky test, whose verdict becomes sticky;
- `TZ`, the locale, the date and the clock, `HYPOTHESIS_*`, and every variable but
  `PYTHON*`, `PYTEST_*` and `CI`;
- the environment changing while the run goes on: a dependency removed and put back
  meanwhile gives kills by a module, kept under a key the environment matches again.

To measure everything again, delete `.invective/cache`.

### The entry

```json
{"campaign": "<64 hex>", "mutant": "<64 hex>", "ok": false, "code": 1,
 "killer": "tests/test_x.py::test_y", "origin": "", "concurrent": false,
 "safe": false}
```

| field | what it holds |
|---|---|
| `campaign`, `mutant` | the whole hashes, both compared on a read |
| `ok`, `code`, `killer` | the verdict as it landed |
| `origin` | `probe` or `coverage` when the remembered killer or the covering tests made the kill (`store.ORIGIN_FLAG`), `""` otherwise |
| `concurrent` | decided by a run beside other copies' runs: more than one worker, and not confirmed with nothing else running |
| `safe` | decided with `--no-unsafe-speedups` on |

What is kept (`store.sound`): a survivor, accepted or not, which only the whole
selection makes; and a kill with a test or module named and pytest's code for a test
that failed or a module that would not import (`ExitCode.TESTS_FAILED`,
`ExitCode.INTERRUPTED`). A kill by time, a narrowed one included, by a signal or a
crash, with another of pytest's codes, or with nothing named is never kept: each can be
the machine's load or memory. Nor is a refusal, a run cut short, or a verdict read back,
which put again would lose its `origin`.

### The read rule

`store.Cache.hit`, one rule for the attempt and for the gate's look ahead (below). An
entry is not read when:

| the entry | the run |
|---|---|
| is not there | any (said nowhere) |
| cannot be read, is not an entry's shape or of this campaign and mutant, or is of a shape no run writes (`safe` with `concurrent` or an `origin`, or a survivor with a code, a killer or an `origin`) | any (counted, and said once on a `warning:` line once the runs are done) |
| is not `store.sound` | any |
| is not `safe` | has the unsafe speedups off |
| is `concurrent` | has one worker |
| is a kill | has `confirm` and more than one worker |
| has `origin` `probe` | has the history off, or `--tests` holding an option |
| has `origin` `coverage` | has coverage off, or coverage not used |

With the switch off, the last four rows apply to every entry; with it on, a run has one
worker and neither attempt, so only what a run with the switch decided is read, even
what a run without it decided at one worker with the history and coverage off. A
survivor that `--confirm` settled at more than one worker is kept as `concurrent`, and
read by later runs with `--confirm`, which never run a survivor again; a kill that
confirmation settled, alone or by the whole selection, is kept as not `concurrent`.

A verdict read back is reported with `"via": "cache"` and its `origin`, lands as any
other does (the history notes its killer), and is never put again. A mutant whose
verdict is read is not probed, so before the gate each remembered mutant's verdict is
looked up, and only the killers of those it does not find are gated. The `history:`
line counts every mutant remembered, and adds how many were read from the cache; the
`apart:` line counts only the gates that ran.

### Resume

Each outcome is put as it lands on the main thread, as one file written whole, so a
SIGKILL, ^C or SIGTERM keeps everything that landed, outcomes that end while the runs
are being stopped included. A put that fails is said once, and no other is tried in the
run.

| the run | put as it lands | what a run after it reads |
|---|---|---|
| one worker | every outcome | all of it, at any count of workers |
| more than one worker, no `confirm` | every outcome, `concurrent` | at more than one worker only |
| more than one worker, `confirm`, stopped before confirming | survivors only | the survivors |
| more than one worker, `confirm`, stopped while confirming | survivors, and the kills confirmed so far | the survivors; those kills without `confirm`, or at one worker |
| refused part way | what landed before the refusal | as above |
| a changed target, selection, setting, interpreter, test or imported file | | nothing: another campaign |
| a kill by time or a signal | never | |

On the way out, whatever ends the run once the key is made, the campaign's directory is
touched, and every other campaign of the target is removed but the `store.KEEP` newest.
Another target's campaigns and the `.gitignore` are not touched, and a campaign removed
under a run that is writing to it is made again by its next put.
