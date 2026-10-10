# Working in this repository

## Changes go through pull requests

Never push to `main`. Every change, however small, goes on a branch of its
own and reaches `main` through a pull request, so that `checks` runs on it
before it lands and the tag `release` puts on `main` is only ever on a green
commit.

- Open the pull request against `main`, and merge it only once `checks` is
  green on its head commit.
- A pull request is merged only when it is finished: thoroughly reviewed,
  every review thread answered, and code that is ready to stay. Green checks
  are necessary, not sufficient. Claude may merge its own pull requests on
  those terms without asking first.
- One piece of work is one pull request. Don't merge a part of it early to
  get something onto `main`.
- A pull request that has been merged is finished. Further work starts a
  fresh branch from the latest `main`, never more commits on the merged one.
- Once a pull request is merged, delete its branch: on the remote always, and
  locally wherever a checkout has it. A branch left behind looks like work
  still in progress.

## Versions and tags

`[project].version` in `pyproject.toml` is the release. When a pull request
changes what a user of invective sees (a flag, a report field, a verdict, a
refusal), it bumps the version in the same pull request, and regenerates
`uv.lock` with `uv lock` rather than by hand, since the lock records
invective's own version. Once that pull request is merged and `checks` is
green on `main`, the `release` workflow tags the commit `v<version>`. A
version that is already tagged is left alone, so a pull request that does not
bump the version releases nothing.

## Documentation

`docs/` is the map, and `head -4 docs/*.md` is its index. `README.md` is
the user's reference.

- Read `docs/documentation.md` before writing a doc, a comment or a
  docstring: it holds the shape, the maxims, and the mechanical checks.
- Read `docs/architecture.md` for the module table, the plugin separation,
  and the handshake.
- Read `docs/isolation.md` before changing the copy, the reaper, the
  signals, or the import-from-outside refusal.
- Read `docs/development.md` for the environment, CI, and how to judge a
  check.
- Read `docs/benchmarking.md` before measuring a speedup.

### Who writes it

Only Claude Opus 4.6 (`claude-opus-4-6`) may write or rephrase
documentation, or any part of it. Any model may make corrections: fixing a
fact that is wrong, a broken reference, a typo -- without rewording the
surrounding prose.

What counts as documentation: `README.md`, `CLAUDE.md`, everything under
`docs/`, plans included. Code comments and docstrings are not covered by
this rule.

### No history

Documentation says what is true now. It never carries history: no
"previously", "was changed", "update:", "decided on (date)", no narrative of
how something came to be. Git is the history.

The one exception is structured findings -- a measurement, an assessment, a
benchmark with its numbers and conditions. Each finding must be isolated from
normal prose and immediately recognisable as a finding, never blended into
the description of how things are. Use this form:

> **Finding:** (date, conditions)
>
> The numbers, and what they show.

A finding carries the date it was taken, the conditions under which it was
taken, the numbers, and the conclusion. It stands on its own, in a block or
under a heading that starts with **Finding:**, so that no reader mistakes it
for current-state documentation.
