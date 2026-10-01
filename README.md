# invective

Mutation testing for a pytest suite. invective makes one small edit to a
module, runs the tests that are supposed to cover it, and reports every edit
the tests did not notice.

A passing suite shows the code does what the tests say. It does not show the
tests say anything. A surviving edit (a *mutant*) is a line whose behaviour
no test depends on: either a check is missing, or the line does not matter.

## Install

It is not on PyPI. Install it from this repository into the project whose
tests you want to measure:

    uv add --dev git+https://github.com/gsfernandes81/invective

It needs Python 3.11 or later, pytest 8.2 or later, and `git` on the PATH.

## One module

    invective run --target src/pkg/gate.py --tests tests/test_gate.py

`--tests` takes anything pytest accepts as a selection, relative to the
repository's top level. `--only CMP,RAISE` limits the kinds of edit,
`--limit 40` caps how many are tried (evenly spaced through the file), and
`--json report.json` writes the full report.

    worktree:  /tmp/invective-k2m1x9
    target:    src/pkg/gate.py
    tests:     tests/test_gate.py
    baseline:  green in 0.4s
    mutants:   6

      SURVIVED  src/pkg/gate.py:4  And -> Or          if member and age >= 65:

    5/6 killed (83.3%), 1 survived

The JSON report also names, for every killed mutant, the first test that
failed on it. Each mutant's run reports that through invective's own pytest
plugin, which it loads.

## From pytest

Installed, invective is also a pytest plugin. Give pytest the module to break
with `--mutate`, and select tests the way you always do:

    pytest --mutate src/pkg/gate.py tests/test_gate.py -k refus

The mutants are run against exactly the tests pytest collected, so `-k`, `-m`,
`--deselect` and node ids all narrow the selection. `--mutate` can be given
more than once; `--mutate-only`, `--mutate-limit` and `--mutate-json` work as
`--only`, `--limit` and `--json` do for `invective run`. The tests are not
run as an ordinary session, so pytest's last line says "no tests ran"; the
report is the section above it.

pytest-xdist's workers have to be off for the outer run (`-n 0`): its
controlling process collects nothing, and an empty selection is refused.
Without `--mutate`, the plugin does nothing.

Only the selection reaches the mutants' runs. Options that change how tests
run, such as `-p`, `-o`, `-W` or `--runxfail`, are not passed on: put them in
the repository's pytest configuration, which every run reads. An option left
behind either makes the run refuse, or leaves survivors it would have killed;
it never adds a kill.

The selection is collected from your checkout and run at the last commit. A
test that is not committed yet makes the run refuse, and an uncommitted edit
to a test is not in the run at all.

## A whole source tree

    invective sweep --src src/pkg --tests-dir tests

Run it from the repository's top level. Each module under `--src` is mutated
against the `test_*.py` files in `--tests-dir` that import it, or that are
named after it. A module no test file imports is listed as **unmeasured**:
it has no score, which is different from a perfect one.

The sweep tries `RAISE` only and at most 25 mutants per module unless told
otherwise with `--only` and `--limit`.

## The edits

| name | what changes |
|---|---|
| `CMP` | a comparison moves by one: `<` and `<=`, `>` and `>=`, `==` and `!=`, `in` and `not in`, `is` and `is not` |
| `BOOL` | `and` becomes `or`, and the reverse |
| `NOT` | a `not` is dropped |
| `CONST` | `True` becomes `False` and the reverse; an integer goes up by one |
| `RAISE` | a `raise` becomes `pass` |

Strings are never edited.

## Reading a survivor

A survivor is a question: does anything depend on this line being as it is?
There are three sound answers. Write the test that checks it, and say what
failure that test guards against. Delete the line, if nothing depends on it.
Or record why neither applies, as with an edit that cannot change behaviour.

invective exits 0 when there are survivors. It exits 2 when the run could not
be trusted, and says why.

## What it will not do

- **It measures the last commit.** Every mutant is written into a temporary
  git worktree of `HEAD`, so the checkout is never edited and nothing is left
  behind if the run is killed. A test you have not committed is not in that
  worktree, and a mutant it would catch goes on surviving until you commit.
- **It refuses a failing selection.** If the chosen tests do not pass before
  any edit, every mutant would count as killed and the score would be 100%
  for nothing. The same goes for a selection that collects no tests.
- **It runs pytest only**, one mutant at a time, in a fresh process each.

## License

GNU Affero General Public License, version 3 only. See `LICENSE`.
