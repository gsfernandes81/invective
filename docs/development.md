# Development

Working on invective itself: environment, the suite, CI, and invective on
its own code; settled.

## Environment

```console
uv sync
uv run pytest
```

The suite runs on pytest-xdist's workers by default (the `addopts` in
`pyproject.toml`). A run with `--mutate` has to be given `-n 0`, since the
controlling process collects nothing and an empty selection is refused.

`tests/conftest.py` puts this checkout's `src` on `sys.path` so the code
under test comes from here, not from an installed copy: a run of invective
against itself tests a copy, and the copy's `src` holds the mutant.

## CI

`.github/workflows/checks.yml` runs the suite on every supported Python
(3.11 to 3.14) on Linux and Windows, and a `lowest-direct` leg on 3.11
with the oldest pytest and pytest-xdist the project allows. The workflow's
comments say what each leg is for.

The `mutation` workflow (`.github/workflows/mutation.yml`) runs invective
on its own code, one module per job. To do that for one module locally:

```console
uv run pytest -n 0 --mutate src/invective/mutate.py tests/test_mutate.py
```

## Judging a check

A new or changed test must be seen red before its green is trusted: run
invective on it, or break the condition it guards and watch it fail. Every
non-obvious check carries one sentence, beside the check, saying what
silent failure it stands for.

## Accepting survivors in invective's own code

A survivor in invective's own `src/` is accepted in the source with an
`invective: accept[reason: change]` comment, as `accept.py`'s docstring
describes. The acceptance says why the mutant may survive.

## Benchmarking

See `docs/benchmarking.md`: one command compares two refs on the benchmark's
cases, and the page says what a pull request's measurement includes.
`bench/` holds the tool and its cases, and is in neither the wheel nor the
sdist.

## The docs test

See "Checks" in `docs/documentation.md`.

## PR and version rules

See `CLAUDE.md`. A pull request that changes what a user sees bumps
`[project].version` in `pyproject.toml` and runs `uv lock` (the `checks`
workflow runs `uv sync --locked`, so a lock not regenerated fails). Once
the pull request is merged and `checks` is green on `main`, the `release`
workflow (`.github/workflows/release.yml`) tags the commit and publishes a
GitHub release with the wheel and sdist.
