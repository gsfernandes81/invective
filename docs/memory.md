# Memory

The `.invective/` directory: the killer history; settled.

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
by the next write once it is older than `store.STALE`.

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
