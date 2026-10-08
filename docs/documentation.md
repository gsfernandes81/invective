# Writing rules

How documentation, comments and docstrings are written here; settled.

## Shape

`docs/` is flat and markdown only. One subject per file, named broad to
narrow and hyphenated, at most three segments. There is no index file:
`head -4 docs/*.md` is the index. A rename must carry every citation.

Lifecycle subdirectories only: `docs/plans/` for designs decided but not
built, `docs/handoff/` for transient session notes. A plan moves into
`docs/` once built. A handoff is written directly and deleted when its open
items close.

## The header

Line 1 is `# <subject>`, then a blank line, then one or two summary lines
saying what the file holds and its state (settled, open, planned,
procedure), then a blank line.

## The maxims

Least-ambiguous word. Cut every word that can go. Loss is spent in order:
never cut a measurement, a hazard, a constraint, a decision's why, or an
open question. Importance is position. The first sentence of a paragraph is
its thesis. Every zoom (heading, summary, first sentence) tells the truth.

The reader has the code open: cite the symbol (`mutate.OPERATORS`,
`tree.SKIPPED`), never paraphrase it. Git is the history: a changed premise
rewrites the paragraph, never an appended "update". A date is part of the
fact: measurements and events keep their date and place, commentary carries
none. Unknown is written as unknown. A warning stands where it bites. The
mistake is shown in its exact wrong form. Examples must run as written.

A comparison is a table. Parallel content, parallel shape. Procedures are
second person and imperative. Asides go in parentheses, not dashes. Prose
wraps at 90 characters.

## Comments and docstrings

A comment says what the code cannot: the constraint, the reason, or the
silent failure a check stands for. A docstring states the contract, never
the implementation. Neither carries the past, which git holds. A fact gone
uncertain is demoted to docs/ and marked uncertain.

## When docs change

A pull request that changes a fact a page states changes the page in the
same pull request. A change that bumps the version updates `README.md`.

## Splitting

A file is one subject when a reader who needs part of it needs most of it.
Size never splits a file. The three-segment name cap is the merge test.

## Staleness

A page whose subject no longer applies is rewritten to what is true now or
deleted. It is never marked stale.

## Checks

`tests/test_docs.py` enforces the mechanical rules: every backticked path
names something that exists, every dotted symbol resolves, every command in
a code block parses, the header shape is right, prose wraps at 90
characters, and line-number citations and dash asides are caught.
