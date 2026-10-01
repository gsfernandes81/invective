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

It needs pytest 8.2 or later, and supports every Python that has not reached
its end of life: 3.11 to 3.14 today. It does not need git, except to run on a
commit with `--ref`.

## One module

Run it from the project's top level:

    invective run --target src/pkg/gate.py --tests tests/test_gate.py

`--tests` takes anything pytest accepts as a selection. `--only CMP,RAISE`
limits the kinds of edit, `--limit 40` caps how many are tried (evenly spaced
through the file), and `--json report.json` writes the full report.

    copy:      /tmp/invective-k2m1x9
    target:    src/pkg/gate.py
    tests:     tests/test_gate.py
    baseline:  green in 0.4s
    mutants:   6

      SURVIVED  src/pkg/gate.py:4  And -> Or          if member and age >= 65:

    5/6 killed (83.3%), 1 survived

The JSON report also names, for every killed mutant, the first test that
failed on it. Each mutant's run reports that through invective's own pytest
plugin, which it loads.

## What is measured

The mutants are written in a copy of the project made as the run starts, so
your files never hold one, and nothing is left behind if the run is killed.
The copy is of the files as they stand: uncommitted edits and new files are
measured. It leaves out version control, caches and virtual environments;
`exclude` in `[tool.invective]` leaves out anything else, such as large data.

To measure a commit instead, give `--ref` (`--mutate-ref` from pytest) a
commit, branch or tag. That is the one use invective makes of git.

## From pytest

Installed, invective is also a pytest plugin. Give pytest the module to break
with `--mutate`, and select tests the way you always do:

    pytest --mutate src/pkg/gate.py tests/test_gate.py -k refus

The mutants are run against exactly the tests pytest collected, so `-k`, `-m`,
`--deselect` and node ids all narrow the selection. `--mutate` can be given
more than once; `--mutate-only`, `--mutate-limit`, `--mutate-json` and
`--mutate-ref` work as `--only`, `--limit`, `--json` and `--ref` do for
`invective run`. The tests are not run as an ordinary session, so pytest's
last line says "no tests ran"; the report is the section above it.

pytest-xdist's workers have to be off for the outer run (`-n 0`): its
controlling process collects nothing, and an empty selection is refused.
Without `--mutate`, the plugin does nothing.

These options reach every mutant's run as you gave them: `-p`, `-o`, `-W`,
`--import-mode`, `--runxfail`, `--strict-markers` and `--doctest-modules`.
Others do not: `--pdb` would stop a run for good, `--lf` would drop tests,
and `-q` would leave a refusal nothing to quote. Your pytest configuration is
in the copy, so every run reads it.

## A whole source tree

    invective sweep --src src/pkg --tests-dir tests

Run it from the project's top level. Each module under `--src` is mutated
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
Or accept it in the source, saying why neither applies:

    tail = out[-400:]  # invective: accept[equivalent: 400 -> 401] any length serves

    # invective: accept[untestable] Windows only, and the sweep runs on Linux
    subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)

The reason is `equivalent`, `untestable` or `out_of_scope`, the same as
pytest-gremlins' pardons, and the text after it is required. A change after
the reason, as the report names it, accepts that one mutant of the line;
without one, every mutant of the line is accepted, including any a later edit
adds. A comment above a line applies to it, and several can stand there.

An accepted mutant is still run. If it is killed, or if no mutant of the line
is the one named, the acceptance is **stale**: what it claims is not so.

## Failing a run on survivors

invective exits 0 when there are survivors, unless the project says otherwise
in its `pyproject.toml`:

    [tool.invective]
    fail-on-survivors = true   # a survivor no comment accepts, or a stale
                               # acceptance, fails the run
    max-accepted = 10          # so do more accepted survivors than this
    exclude = ["var/*"]        # left out of the copy

A run that breaks these rules exits 1 (`pytest --mutate` fails as a failing
test would). A run that could not be trusted exits 2, and says why. An unknown
key in `[tool.invective]` is refused, so a misspelt one is not read as absent.

## What it will not do

- **It refuses a failing selection.** If the chosen tests do not pass before
  any edit, every mutant would count as killed and the score would be 100%
  for nothing. The same goes for a selection that collects no tests.
- **It runs pytest only**, one mutant at a time, in a fresh process each.

## Developing invective

    uv sync
    uv run pytest

The suite runs on pytest-xdist's workers by default. CI runs it on every
supported Python on Linux and Windows, and runs invective on its own code
(the `mutation` workflow), one module per job. To do that for one module here:

    uv run pytest -n 0 --mutate src/invective/mutate.py tests/test_mutate.py

## License

GNU Affero General Public License, version 3 only. See `LICENSE`.
