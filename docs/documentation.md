# Writing rules

How documentation, comments and docstrings are written here; settled.

## Shape

`docs/` is flat and markdown only. One subject per file, named broad to
narrow and hyphenated, at most three segments. There is no index file:
`head -4 docs/*.md` is the index. A rename must carry every citation.

Lifecycle subdirectories only: `docs/plans/` for designs decided but not
built. A plan moves into `docs/` once built.

## The header

Line 1 is `# <subject>`, then a blank line, then one or two summary lines
saying what the file holds and its state (settled, open, planned,
procedure), then a blank line.

## The maxims

Cite the symbol (`mutate.OPERATORS`, `tree.SKIPPED`), never paraphrase it.
No history (git holds it); measurements only as `**Finding:**` blocks per
`CLAUDE.md`. A comparison is a table. Asides in parentheses, not dashes.
Prose wraps at 90 characters. Examples run as written.

## Comments and docstrings

A comment says what the code cannot: the constraint, the reason, or the
silent failure a check stands for. A docstring states the contract, never
the implementation. Neither carries the past, which git holds. A fact gone
uncertain is demoted to docs/ and marked uncertain.

## When docs change

A pull request that changes a fact a page states changes the page in the
same pull request. A change that bumps the version updates `README.md`.

## Staleness

A page whose subject no longer applies is rewritten to what is true now or
deleted. It is never marked stale.

## Checks

`tests/test_docs.py` enforces the mechanical rules and reads the prose's
restatements of the code. Mechanical checks: paths exist, symbols resolve,
fenced command lines parse, the header shape is right, prose wraps at 90,
no line-number citations, no dash asides. Restatement checks: the edits
table, CMP pairs, sweep defaults, reasons, acceptance examples, settings
tables, `--mutate-*` pairings, forwarded options, handshake variables and
`tree.SKIPPED` each match their source in the code. Every scan asserts it
found something, so a pattern that stops matching fails rather than
passing empty.
