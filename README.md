# invective

Mutation testing for a pytest suite. invective replaces one function body
at a time with a value of its return type, runs the tests that cover that
function, and reports every replacement the tests did not notice.

A passing suite shows the code does what the tests say. It does not show the
tests say anything. A surviving replacement (a *mutant*) is a function whose
result no test depends on: either a check is missing, or the function does
not matter.

The 1.0 engine is being built on this branch: `docs/plans/rewrite.md` holds
the decisions and what done means. Until it lands, install the 0.5.1 release
from the repository's tags.

How it works and why: `docs/` in a clone (`head -4 docs/*.md`).

## Running it

From inside the project, with invective installed beside pytest:

```console
pytest -n 0 --mutate src/pkg/gate.py tests/test_gate.py
```

Each `--mutate` names a file or a directory of the project to mutate; the
rest of the command line is pytest's own selection of tests. `--mutate-json
report.json` writes every mutant with its verdict.

## License

GNU Affero General Public License, version 3 only. See `LICENSE`.
