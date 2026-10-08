# Gitignored files

Two proposed `[tool.invective]` keys for leaving out gitignored files;
proposed, not decided.

## The keys

```toml
[tool.invective]
ignore-gitignored = false
include-ignored   = [".env", "src/pkg/_version.py", "build/*.so"]
```

`ignore-gitignored = true` skips every file git reports as ignored, using
`git ls-files --others --ignored --exclude-standard`, so it needs git and
refuses outside a repository. Untracked files that are not ignored are
still copied. `include-ignored` re-includes a subset of the ignored files
and does nothing when `ignore-gitignored` is false; `exclude` still wins
over it.

## Why the default is false

Gitignored files are sometimes load-bearing: `.env`, a generated
`_version.py`, in-place `.so` builds, fixture data. A file that was left
out and was needed fails the baseline, which is a refusal and not a wrong
score, and its message should name the option.
