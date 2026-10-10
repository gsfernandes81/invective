# invective

Mutation testing for a pytest suite. invective makes one small edit to a
module, runs the tests that are supposed to cover it, and reports every edit
the tests did not notice.

A passing suite shows the code does what the tests say. It does not show the
tests say anything. A surviving edit (a *mutant*) is a line whose behaviour
no test depends on: either a check is missing, or the line does not matter.

How it works and why: `docs/` in a clone (`head -4 docs/*.md`).

## Install

It is not on PyPI. Install it from this repository into the project whose
tests you want to measure (it goes into the project's virtual environment,
beside pytest):

```console
uv add --dev git+https://github.com/gsfernandes81/invective
```

To pin a release by tag:

```console
uv add --dev git+https://github.com/gsfernandes81/invective@v0.3.0
```

Or install the release's wheel, which needs no build backend:

```console
uv add --dev https://github.com/gsfernandes81/invective/releases/download/v0.3.0/invective-0.3.0-py3-none-any.whl
```

It needs pytest 8.2 or later in the same environment, and supports every
Python that has not reached its end of life: 3.11 to 3.14 today. It does not
need git, except to run on a commit with `--ref`.

## One module

Run it from anywhere inside the project (the nearest directory at or above
the working directory that holds a `pyproject.toml` or another pytest
settings file; with none, the working directory itself):

```console
invective run --target src/pkg/gate.py --tests tests/test_gate.py
```

`--target` and `--tests` are relative to the directory the command is run
in. `--tests` takes files, directories and node ids. `--only CMP,RAISE`
limits the kinds of edit, `--limit 40` caps how many are tried (evenly
spaced through the file), and `--json report.json` writes the full report.
`--ref` runs on a git commit, branch or tag instead of the files as they
stand.

`--workers N` runs N mutants at once, each in a copy of its own; `auto` is
one per available CPU, at most 8. Either is at most one per mutant, and the
`workers:` line says how many ran. `workers` in `[tool.invective]` sets the
default, which is 1.

With more than one worker, the suite has to be one whose tests are
independent of their order and safe to run in parallel, the bar pytest-xdist
sets. A test that shares a port, a fixed path or a database with its copy in
another worker's run, or that needs another test to run before it, can kill a
mutant or let one survive because of what else ran, and for such a suite the
verdicts are not guaranteed. A selection green alone and red when every copy
runs it at once is refused for that.

`--confirm` (`confirm` in `[tool.invective]`) looks for such tests. Once the
last mutant has run, every kill is run again with nothing else running: its
killer alone, or else the whole selection, whose verdict stands. The entry
carries `confirmed` (`alone` or `full`) in the report, a survivor's too when the
whole selection let the mutant through. A kill its killer does not make alone
is listed under `unreproduced` and in the closing lines, with that killer, the
test to look at; so is a kill with no killer to run alone (a kill by time, by a
module, or under `--tests` holding an option) that the whole selection lets
through. With one worker there is nothing to confirm.

```text
copy:      /tmp/invective-k2m1x9ab
workers:   1
project:   /home/me/proj
target:    src/pkg/gate.py
tests:     tests/test_gate.py
baseline:  green in 0.4s
mutants:   6

  SURVIVED  src/pkg/gate.py:4  And -> Or                    if member and age >= 65:

5/6 killed (83.3%), 1 survived
```

The mutant is the file with that one span edited, so a traceback from a
killed run points at the file's own lines; an entry that carries
`"whole_file": true` is the exception and is flagged.

The JSON report also names, for every killed mutant, the first test that
failed on it. Each mutant's run reports that through invective's own pytest
plugin. Every entry (survivor, kill or accepted) carries a `diff`: a unified
diff of the source file against the mutant.

## What is measured

The mutants are written in a copy of the project (one per worker) made as the
run starts, so your files never hold one. A run stopped by ^C or SIGTERM
removes its copies; one killed outright leaves them marked with their owner,
which the next invective to start removes. The copy is of the files as they
stand: uncommitted edits and new files are measured. It leaves out version
control, caches and virtual environments; `exclude` in `[tool.invective]`
leaves out anything else, such as large data.

To measure a commit instead, give `--ref` (`--mutate-ref` from pytest) a
commit, branch or tag. That is the one use invective makes of git.

## From pytest

Installed, invective is also a pytest plugin. Give pytest the module to break
with `--mutate`, and select tests the way you always do:

```console
pytest --mutate src/pkg/gate.py tests/test_gate.py -k refus
```

The mutants are run against exactly the tests pytest collected, so `-k`, `-m`,
`--deselect` and node ids all narrow the selection. `--mutate` can be given
more than once; `--mutate-only`, `--mutate-limit`, `--mutate-json`,
`--mutate-ref`, `--mutate-workers` and `--mutate-confirm` work as `--only`,
`--limit`, `--json`, `--ref`, `--workers` and `--confirm` do for `invective
run`. The tests are not run as an ordinary session: pytest's last line says
how many were deselected or that no tests ran. The report is the section above
it.

pytest-xdist's workers have to be off for the outer run (`-n 0`): with
workers on, `--mutate` stops at startup with a usage error (exit 4).
Without `--mutate`, the plugin does nothing.

These options reach every mutant's run: `-p`, `-o`, `-W`, `--import-mode`,
`--runxfail`, `--strict-markers` and `--doctest-modules`. `-c FILE` is also
forwarded, given from the project's top; a run given `-c` skips the settings
check. The rest do not reach the mutants: `--pdb` would stop a run for good,
`--lf` would drop tests, and `-q` would leave a refusal nothing to quote.
Your pytest configuration is in the copy, so every run reads it.

A child interpreter started by a test imports the mutant only when it starts
from the copy's working directory (or when the test puts the copy on
`sys.path`). In a `src` layout, starting the child from elsewhere imports the
installed package, not the copy. Issue #2 tracks the known limits.

## A whole source tree

```console
invective sweep --src src/pkg --tests-dir tests
```

Run it from anywhere inside the project; `--src`, `--tests-dir` and
`--modules` are relative to the directory it is run in. Each module under
`--src` is mutated against the `test_*.py` files in `--tests-dir` (and its
subdirectories) that import it, or that are named after it. A module no test
file imports is listed as **unmeasured**: it has no score, which is different
from a perfect one.

The sweep tries `RAISE` only and at most 25 mutants per module unless told
otherwise with `--only` and `--limit`. `--json` writes the results as JSON;
each measured entry carries a `report` key with the engine's full report for
that module (survivors, accepted, stale and kills as data, with `diff`).
`--ref` runs on a git commit, branch or tag. `--workers N` gives each module's
engine N workers.

`--modules` sweeps only the named module files instead of discovering them
under `--src`:

```console
invective sweep --src src/pkg --tests-dir tests --modules src/pkg/gate.py src/pkg/auth.py
```

`--src` and `--tests-dir` are still required with it. A bare `--modules`
with no values sweeps no module (exit 0), which is what
`--modules $(git diff --name-only ...)` does when nothing changed.

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

```python
tail = out[-400:]  # invective: accept[equivalent: 400 -> 401] any length serves
```

```python
# invective: accept[untestable] Windows only, and the sweep runs on Linux
subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
```

The reason is `equivalent`, `untestable` or `out_of_scope`, and the text
after it is required. A change after
the reason, as the report names it, accepts that one mutant of the line;
without one, every mutant of the line is accepted, including any a later edit
adds. A comment above a line applies to it, and several can stand there.

An accepted mutant is still run. If it is killed, or if no mutant of the line
is the one named, the acceptance is **stale**: what it claims is not so.

## Failing a run on survivors

invective exits 0 when there are survivors, unless the project says otherwise
in its `pyproject.toml`:

```toml
[tool.invective]
fail-on-survivors = true   # a survivor no comment accepts, or a stale
                           # acceptance, fails the run
max-accepted = 10          # so do more accepted survivors than this
exclude = ["var/*"]        # left out of the copy
workers = 1                # mutants run at once; "auto" for one per CPU
confirm = false            # with workers, confirm each kill alone
```

| command | exit code | meaning |
|---|---|---|
| `invective run` / `invective sweep` | 0 | completed (survivors are a finding, not a failure, unless the project's rules say otherwise); the sweep also exits 0 for a RED baseline, "no mutation sites" or a driver failure on a single module |
| `invective run` / `invective sweep` | 1 | a module broke the project's rules (`fail-on-survivors`, `max-accepted`) |
| `invective run` / `invective sweep` | 2 | a refusal (red baseline, empty selection, target loaded from outside the copy, pytest settings above the project's top); the sweep also exits 2 when any module is imported from outside the copy |
| `invective run` | 143 | SIGTERM (128 + signal number in a shell) |
| `pytest --mutate` | 1 | rules broken |
| `pytest --mutate` | 2 | a refusal or SIGTERM |
| `pytest --mutate` | 4 | a usage error (xdist workers on, unknown `--mutate-only` kind) |

An unknown key in `[tool.invective]` is refused, so a misspelt one is not
read as absent.

## What it will not do

- **It refuses a failing selection.** If the chosen tests do not pass before
  any edit, every mutant would count as killed and the score would be 100%
  for nothing. The same goes for a selection that collects no tests.
- **It runs pytest only**, in a fresh process for each mutant.

## Developing invective

See `docs/development.md`.

## License

GNU Affero General Public License, version 3 only. See `LICENSE`.
