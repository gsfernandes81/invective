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
