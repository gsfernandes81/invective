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

## The docs test

`tests/test_docs.py` checks that the documentation stays in step with the
code: every backticked path names something that exists, every dotted
symbol (`mutate.OPERATORS`, `tree.SKIPPED`) resolves by import, every
`invective run` or `invective sweep` line in a fenced code block parses
against the real parser, and every `pytest --mutate` option is one the
plugin registers. It also enforces the mechanical rules from
`docs/documentation.md`: the header shape, the 90-character prose wrap, and
the absence of line-number citations and dash asides.

## PR and version rules

See `CLAUDE.md`. A pull request that changes what a user sees bumps
`[project].version` in `pyproject.toml` and runs `uv lock` (the `checks`
workflow runs `uv sync --locked`, so a lock not regenerated fails). Once
the pull request is merged and `checks` is green on `main`, the `release`
workflow (`.github/workflows/release.yml`) tags the commit and publishes a
GitHub release with the wheel and sdist.
