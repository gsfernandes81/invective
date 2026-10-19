# Verdict harness

The three-arm harness that checks the engine's verdicts against plain pytest on every
fixture project, and how to add a case or retire an expected disagreement; a procedure.

## What it checks

The harness gives every mutant of a fixture project three verdicts, reached three
independent ways, and the project's case fails when they disagree in a way its
`expected.toml` does not list. A case is one project under `tests/projects/`, and
`tests/test_verdicts.py` runs one test per project.

The three arms:

- **Arm 1, the engine.** `pytest --mutate` on the project's targets, with forked
  children, its verdicts read from its JSON report. It runs with one worker, and again
  with each further worker count the project's file asks for.
- **Arm 2, the subprocess path.** Per mutant, a fresh pytest without `--mutate`, with
  the mutant named in `INVECTIVE_MUTANT` as `<path>:<ordinal>`. In such a run the
  plugin installs the finder for that file and makes that mutant live before any
  conftest is imported (`plugin.pytest_load_initial_conftests`). It runs the tests
  arm 1 found covering the mutant, in arm 1's order, or every test when arm 1 gave no
  verdict or left the mutant unmeasured.
- **Arm 3, ground truth.** Per mutant, a copy of the project with the mutant written
  into the target as a text edit, and plain pytest with invective's plugin turned off,
  over every test. Nothing of invective's runs in it.

Arm 2 shares the engine's instrumenter and switch but not its forks, and arm 3 shares
neither. So a disagreement between arms 1 and 2 points first at the fork path or at
how the engine reads its children, and one between arms 2 and 3 at the instrumented
code. Treat that as where to look first, not as a diagnosis.

### What each arm can say

| arm | its verdict words |
|---|---|
| 1 | killed, survived, unreached, unmeasured |
| 2 | killed, survived, timeout, refused, crashed |
| 3 | killed, survived, unreached, timeout, crashed |

Arm 1 can also fail as a whole: it is `refused` when invective refuses the run, and
`crashed` when it times out, exits other than 0, or writes no report. The engine's
own words keep the meanings of decision 7 in `docs/plans/rewrite.md`. Arm 2 does not
measure reach, so its `survived` covers what arm 1 calls `unreached`. Arm 3's
`unreached` is a survivor whose function never ran in the probe run below.

### How arms 2 and 3 are judged

Arms 2 and 3 are judged against arm 3's baseline, one unmutated plain pytest run of
the same command. A test that passed there and fails or errors in any phase now kills
the mutant, and the first such test is named; a collection that fails kills it too. A
test that is skipped or never starts does not kill. The baseline is green when pytest
exits 0 having run a test; on a red baseline no mutant is run and only the project
level is compared.

A run that ends inside a test (a test that calls `os._exit`) kills the mutant through
that test, and the verdict is marked as not plain. The harness can name that test
because a small plugin of its own (`tests/harness/recorder.py`) writes each test's
start, each report and the exit status as they happen, one line to a file, opening
and closing the file each time. Pytest's summary is never printed by such a run, and
a test that closes the process's descriptors cannot lose the lines already written.

A run past its wall limit is a `timeout`. The limit is ten times the baseline's wall
time and at least ten seconds (`arms.limit_of`); arm 1 has 900 seconds for its whole
run.

### Copies and the mutant table

The harness copies the project once before any arm runs and takes the mutant table
from that copy. Every run of every arm then gets a fresh copy of the first one. A test
that edits the project's source therefore changes no arm's idea of the mutants, and no
run sees what another run wrote.

### How arm 3 writes a mutant

Arm 3 replaces the function's running statements with `return <value>`. The running
statements start after the docstring and any `global` or `nonlocal` declarations and
run to the end of the function; the `def` line, the decorators, the docstring and the
declarations stay as written (`edit.mutate`). A generator's mutant yields its one
value and returns, or returns with an unreachable `yield` after it so that it stays a
generator. A one-line function takes its statements on that same line, joined by
semicolons.

### The probe run

Before the per-mutant runs, the harness runs plain pytest once with a probe at the top
of every target function. Each probe creates a file the first time its function runs,
marked by whether a test was running then. A survivor in arm 3 whose function never
ran there becomes `unreached`. A function seen running outside any test (at import,
for one) is what rule 3 checks arm 1's import-time note against. If the probes turn
a green baseline red, the harness stops with an error rather than a table.

## What counts as a disagreement

Each mutant is keyed by its target's path, as the project names it, and its ordinal
in that file's table, counted from 1. The display id is never the key, since two
mutants can share one. A disagreement is a mutant on which one of these rules fires:

1. One arm says `killed` and another `survived`.
2. Arm 1 says `unreached` and arm 3 kills.
3. Arm 1 says `unmeasured`, arm 3 gives a plain kill or survivor, and arm 1's note
   gives no reason the plan accepts. Two are accepted: the function ran at import,
   when the probe saw it run outside any test; and the run went over its wall budget.
4. Arm 1 refused or crashed on a green baseline, or ran on a red one. This one is for
   the whole project.
5. Arms 1 and 2 give different verdicts. Arm 1's `unreached` against arm 2's
   `survived` agrees, and arm 1's `unmeasured` is left to rule 3, since arm 2 measures
   an import-time mutant that arm 1 leaves unmeasured by design.
6. Arms 1 and 3 give different verdicts that no rule above covers, such as a kill
   where arm 3 finds the function never ran.

The case passes when the disagreements found equal those `expected.toml` lists, in
both directions. A disagreement the file does not list fails the case, and so does an
entry the run does not reproduce.

## Reading the table

The case prints a table: one row per mutant and arm 1 worker count, with the mutant
as path and ordinal, its function, its replacement value, and each arm's verdict. The
last column names the rules that fired, or says `agree`. A note follows for every
verdict other than a kill that carries one, such as why arm 1 left a mutant
unmeasured.

When the case fails, the lines after `against the expected disagreements` say why:

- `unexpected:` a mutant whose rules fired and that no entry names.
- `expected, not seen:` an entry whose mutant agreed, or disagreed with other
  verdicts than the entry says; the line shows what was found.
- `expected entry names no mutant:` an entry whose path, ordinal or function matches
  nothing in the table.
- `project:` arm 1's project level differs from the file's `[project]`.

## Running it

Run one project with `-n 0`, so that the case runs in this process, and `-s`, so that
the table prints on a pass too:

```
uv run pytest tests/test_verdicts.py -k dup -n 0 -s
```

`-k` matches substrings: `-k ns` also selects `options`. When a name is part of
another, give the case's whole node id, which ends in the project's name in brackets.
Without `-k` the file runs every project, spread over the suite's workers.

Each case starts its own pytest processes, half the CPUs at a time and at least two;
`INVECTIVE_HARNESS_JOBS` sets the number.

### An external project

Point the harness at a project outside the tree, such as a benchmark project, with
these variables. The python running the suite runs every arm, so the project's
dependencies and invective must be importable from it.

```
export INVECTIVE_HARNESS_PROJECT=/path/to/project
export INVECTIVE_HARNESS_TARGETS=pkg/module.py
export INVECTIVE_HARNESS_ARGS=tests/test_module.py
uv run pytest tests/test_verdicts.py -k external -n 0 -s
```

- `INVECTIVE_HARNESS_PROJECT` adds the `external` case.
- `INVECTIVE_HARNESS_TARGETS` holds the paths to mutate, relative to the project,
  separated by spaces.
- `INVECTIVE_HARNESS_ARGS` holds further pytest arguments for every run of every arm,
  split as a shell would split them.
- `INVECTIVE_HARNESS_EXPECTED` names an expected-disagreement file. Without it, none
  is expected. The file's `targets` key is not read here; the variable above is.

## Adding a case

Add a case when the engine gives a verdict you believe is wrong. The case passes with
the disagreement listed in `expected.toml`, and fails once a fix removes it.

1. Make the smallest project that shows the fault, as a directory of its own under
   `tests/projects/`. Copy the layout of `tests/projects/dup`: a `pyproject.toml` with
   `pythonpath = ["src"]` and `testpaths = ["tests"]`, the code in a package under
   `src` with the directory's name (the default target, below), and the tests in
   `tests`.
2. Run the case once. Its table lists every mutant, with its function and replacement
   value.
3. Establish each verdict by hand. For each function and each replacement value,
   read the tests and decide whether a test that passes today would fail if the
   function returned that value, and whether any test calls the function at all.
   To test one by hand, replace the function's running statements in a copy with
   `return <value>`, and run `python -m pytest -p no:invective` there with the python
   that runs the suite.
4. Write each verdict and its reason into the function's docstring: "Killed by both
   values: tests check an open hour and closed ones.", "Unreached: no test calls
   it.". The module's docstring says what the project is for and, where it helps,
   what the engine does wrong on it.
5. Compare arm 3's column with the docstrings. The harness reads no docstring, so
   nothing else catches one that disagrees with arm 3. A mismatch means the reasoning
   or the project is wrong; fix that before writing any entry.
6. Write `expected.toml` beside the project, with one entry for each disagreement the
   engine shows today (the format is below), and run the case until it passes. A
   project on which the arms agree needs no file.

An entry's `reason` says, in one sentence, what the engine does wrong: "the two `_`
overloads share one id, and the last verdict read wins." A disagreement the plan
intends, such as a refusal a decision requires, says so: "by design: the canary's
first failure is test_read's order dependence, not the canary." A comment at the top
of the file can carry what one reason cannot.

## The expected-disagreement file

`expected.toml` has three parts, each optional. A missing file expects no
disagreement, and an unknown key, a word that is not a verdict, or an entry without a
reason or a mutant is an error (`compare.load`).

`[harness]` says how to run the project:

- `targets`: the paths the arms mutate, relative to the project. By default, the
  package under `src` named as the project is.
- `workers`: further worker counts arm 1 runs with, besides 1.
- `links`: files that must be symbolic links. Without them the case is skipped, for a
  checkout made without symbolic links.

`[project]` expects a disagreement of the whole project: `engine`, either `refused`
or `crashed`, and `reason`.

Each `[[disagreement]]` expects one, or one per mutant of a function:

- `path`: the target as the project names it.
- `ordinal`, or `function` with an optional `value`. A function without a value names
  every mutant of that function.
- `workers`: the arm 1 worker count it holds for, 1 by default. A count named here is
  run even when `[harness]` does not list it.
- `engine`, `subprocess`, `truth`: the verdict each arm gives. An arm left out is not
  compared.
- `reason`.

```toml
[[disagreement]]
path = "src/dup/m.py"
ordinal = 5
engine = "killed"
truth = "unreached"
reason = "the two `_` overloads share one id, and the last verdict read wins"
```

Name a mutant by `function` when the qualname is unique in its file: an ordinal is a
position in the table, and a function added above it moves it. Use `ordinal` when two
functions share a qualname, as two `singledispatch` overloads both named `_` do, since
a `function` entry would also name the other one's mutants. A `value` is spelled as
the table's value column shows it, quotes included for a string (`"'xyzzy'"`).

## Retiring an entry

An entry retires when the engine stops showing its disagreement.

1. After the engine fix, run the case. The fixed mutants show as `expected, not seen`,
   with the verdicts found, and a fixed project as a `project:` line.
2. Check that those verdicts now agree with arm 3. A mutant that still disagrees, by
   other verdicts, is reported as `unexpected` as well; the fix is not finished.
3. Delete the entry, or the `[project]` table, and any comment in the file about it.
   An entry by `function` of which only some mutants now agree is narrowed to the
   rest, by `value` or `ordinal`. Delete the file when nothing is left in it.
4. Rewrite any docstring in the project that describes the engine's wrong verdict, so
   that it says what is true now.
5. Run every case, not only this one, since an engine fix can change other projects.

An entry whose reason says the disagreement is by design retires only when the plan's
decision changes, not with an engine fix.
