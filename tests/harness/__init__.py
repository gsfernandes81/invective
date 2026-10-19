"""The verdict-equality harness: three arms per mutant, compared, per project.

Arm 1 is the engine's fork path, arm 2 a fresh pytest per mutant with the mutant
named in `environment.MUTANT`, and arm 3 ground truth: the mutant applied as a text
edit to a copy of the project and plain pytest run on it (`arms`). The comparison
keys every mutant by (path, ordinal), fires the rules in `compare`, and passes
when the disagreements found are the ones the project's `expected.toml` lists
(`compare.load`). `check.check` runs one project.

`tests/test_verdicts.py` runs it on every project under `tests/projects/`, and on
an external project when `PROJECT` names one: a by-hand run, for instance on a
benchmark project or on invective's own modules.

    export INVECTIVE_HARNESS_PROJECT=/path/to/project
    export INVECTIVE_HARNESS_TARGETS=pkg/module.py
    export INVECTIVE_HARNESS_ARGS=tests/test_module.py
    python -m pytest tests/test_verdicts.py -k external -s -n 0

The python running the suite runs every arm, so the project's dependencies and
invective must be importable from it.
"""

#: An external project's directory, which adds the `external` case.
PROJECT = "INVECTIVE_HARNESS_PROJECT"

#: The external project's targets, relative to it, separated by whitespace.
TARGETS = "INVECTIVE_HARNESS_TARGETS"

#: Further pytest arguments for every run of the external project, as a shell would
#: split them.
ARGS = "INVECTIVE_HARNESS_ARGS"

#: The external project's expected-disagreement file; by default, none is expected.
EXPECTED = "INVECTIVE_HARNESS_EXPECTED"

#: How many pytest processes arms 2 and 3 run at once; by default half the CPUs.
JOBS = "INVECTIVE_HARNESS_JOBS"
