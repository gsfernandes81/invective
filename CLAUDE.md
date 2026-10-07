# Working in this repository

## Changes go through pull requests

Never push to `main`. Every change, however small, goes on a branch of its
own and reaches `main` through a pull request, so that `checks` runs on it
before it lands and the tag `release` puts on `main` is only ever on a green
commit.

- Open the pull request against `main`, and merge it only once `checks` is
  green on its head commit.
- Claude may merge its own pull requests once they are green and their
  review threads are answered; it does not need to ask first.
- A pull request that has been merged is finished. Further work starts a
  fresh branch from the latest `main`, never more commits on the merged one.

## Versions and tags

`[project].version` in `pyproject.toml` is the release. When a pull request
changes what a user of invective sees (a flag, a report field, a verdict, a
refusal), it bumps the version in the same pull request, and regenerates
`uv.lock` with `uv lock` rather than by hand, since the lock records
invective's own version. Once that pull request is merged and `checks` is
green on `main`, the `release` workflow tags the commit `v<version>`. A
version that is already tagged is left alone, so a pull request that does not
bump the version releases nothing.
