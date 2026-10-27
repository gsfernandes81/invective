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

## The dev container

`invective-dev` is the owner's container for working on invective, one of the dev
containers on the infra repo's hub. infra builds it from this repo's `main` on GitHub, so
a change to the three files below reaches the container once it is merged and infra
rebuilds it. Starting it, logging in, its volumes and the hub are infra's, described in
[infra's dev README](https://github.com/gsfernandes81/infra/blob/main/dev/README.md).

| File | What it does |
|---|---|
| `Dockerfile.dev` | adds the uv that `checks` pins and a venv holding the locked dependencies |
| `docker-child-init.dev.sh` | at every start, adds invective itself to that venv, editable |
| `docker-login.dev.sh` | reports the GitHub token and logs Claude in; infra's `make login` runs it |

Two things are kept equal by hand. The uv version in `Dockerfile.dev` is the `version:` in
`.github/workflows/checks.yml`, so a lock regenerated in the container is one `checks`
accepts. Both syncs pass `--all-extras`, because `uv sync` uninstalls what it is not asked
for, and a start with fewer flags would remove the coverage extra the build installed.

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

A local run keeps each killer in `.invective/` at the checkout's top
(`docs/memory.md`), and the next one tries it first. The workflow's runs start
with none.

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
