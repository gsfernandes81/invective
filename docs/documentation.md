# Writing rules

The rules all prose is written to (docs, comments, docstrings) and the pipeline that
writes docs: shape, names, headers, maxims, staleness; settled.

## Shape

- docs/ is flat, .md only, one file per subject. A topic folder re-asks "which
  folder owns this?" at every write, and a nested name cited bare loses its
  context; names do the grouping instead.
- `plans/` is the one subdirectory, and it marks a lifecycle, never a topic. It holds
  a design for a feature that is decided but unbuilt, moved to docs/ proper once
  built or deleted once dropped. There is no `handoff/`.
- Nothing that is not markdown lives under docs/. A build asset, probe or bundle
  a doc cites is linked where it lives.
- There is no index. The headers are the index: `head -4 docs/*.md`. An index
  file is a second spelling of every header, and it disagrees with them the first
  time a file changes without it.

## Names

- A file is named by its subject, broad to narrow, hyphenated:
  `isolation-signals.md`. Sorted listings group families for free, and the bare
  name cited anywhere still says what it is about.
- Three segments is the cap. A name that wants a fourth names a doc that belongs
  inside its parent subject.
- A file is renamed the moment its name stops stating its subject. The rename
  carries every citation with it (`tests/test_docs.py` goes red on the paths
  missed).

## The header

- The first line is `# <subject>`, then a blank line, then at most two summary
  lines: what the file holds and its state (settled, open, planned, or a
  procedure). A blank line follows before the body, so `head -4` never shows body
  text whether the summary took one line or two.
- The summary is written for one decision, "do I need this file for my task?",
  and never restates the filename.
- Where the project uses a word in more than one sense, the summary says which
  sense it means, and names where the other one lives.

## The maxims

The stylistic law. Maxims bind reviewers, cold readers, and this file; a maxim
never enters a writer's prompt (see The pipeline for why).

- **Ambiguity is anathema.** Always use the least ambiguous word in a sentence by
  searching for misinterpretations.
- **Verboseness is dilution.** If a word can be cut from a sentence while
  retaining its intended meaning, it does not belong in that sentence. The same
  applies to a sentence in a paragraph, a paragraph in a body of text, and a body
  of text in the repo.
- **Loss is spent in order.** Cut the rederivable first, then reasoning a check
  already carries. Never cut a measurement, a hazard, a constraint, a decision's
  why, or an open question. Unsure means it stays.
- **Importance is position.** Important information stands at the front of the
  sentence; important paragraphs toward the start of the body.
- **The opening is the thesis.** If a paragraph were lost, its first sentence
  keeps its most important point; every later sentence supports, qualifies, or
  details the first. Split paragraphs until this holds.
- **Every zoom tells the truth.** A heading makes sense without its body, the
  summary without its file, a first sentence without its paragraph. A reader who
  stops early was told less, never wrong.
- **The reader has the code open.** State only what they cannot derive from it.
  Link code, never paraphrase it (a paraphrase is born drifting).
- **Git is the history.** Docs hold facts learned in the past, never the past
  itself. A changed premise rewrites the paragraph, never an appended update,
  correction, or "outdated" mark.
- **A date is part of the fact.** A measurement or an event keeps its date and
  where it happened, spelled as a `**Finding:**` block (see "No history" in
  `CLAUDE.md`); commentary never carries one, because `git blame` dates every
  sentence already.
- **Unknown is an answer.** A mechanism nobody knows is written as unknown, never
  written around.
- **A warning stands where it bites.** It names its concrete failure at the exact
  step, line, or setting it applies to; its reasoning may take its own section
  below.
- **The mistake is shown, not described.** A known mistake appears in its exact
  wrong form (the command, the config line) followed by what it silently does.
- **An example outranks its description.** Complete enough to run or perform as
  written. A fragment that cannot run is prose wearing a code block.
- **A comparison is a table.** Options weighed against the same criteria never
  appear as paragraphs.
- **Parallel content, parallel shape.** Entries of one kind keep the same parts
  in the same order.
- **The reader came to act or to understand.** A procedure speaks to the actor:
  second person, imperative, common case first, stopping where the actions stop.
  A decision record argues, and its causal chain stays in one sentence even when
  long.
- **Cite the symbol, never the line.** `mutate.OPERATORS` outlives its line
  number, greps cleanly, and fails loudly on rename; a line number drifts
  silently and nothing checks it. A line number is allowed only as a dated
  verification note.
- **An aside sits in parentheses.** Not between dashes, and never nested.

Prose lines wrap at 90 characters; table rows and code blocks are exempt.

## Comments and docstrings

- A comment states what the code cannot say: the constraint, the reason, the
  silent failure a check stands for. Never what the next line does, where code
  came from, or why a change was correct. That is the writer talking to a
  reviewer, and it is noise the moment the change merges.
- A docstring states the contract (what a caller may rely on), never the
  implementation, which the reader has directly below it.
- Code comments hold only current, certain facts. A fact gone uncertain is
  demoted to docs/ and stated as uncertain, in words; a dead one is deleted.
- The maxims apply whole.

## The pipeline

How a doc is written or rewritten. The pipeline is the only way a file under
docs/ is written; `plans/` is the exception, and a plan is written directly. Any
model may make a correction directly (a wrong fact, a broken reference, a typo)
without rewording the surrounding prose. The runner is
`.claude/workflows/docs-pipeline.js`, its seats are `.claude/agents/sighted-seat.md`
and `.claude/agents/blind-writer.md`, and it never lands a file: landing is a
person's act.

- A sighted writer drafts from the tree and the caller's intent, under a
  register brief, never rules: write it the way you would explain it aloud to
  a colleague, then tighten; state only what the reader cannot derive from the
  code, and never paraphrase it. The runner pins the writer to `claude-opus-4-6`.
- The caller's intent carries a thesis (what matters most on the page) and
  the writer leads with it. What git already holds earns body space only as a
  distilled lesson; an annex may keep dated history; deletion is often the
  correct edit.
- Mechanical gates run before any checking seat and are code, not judgment:
  header shape, the 90-character prose wrap, size ceilings, no dash asides
  (parentheses carry them), no line-number citations, and no prose addressed
  to anyone but the page's reader. A guard whose predicate a unit test can
  evaluate is code; a guard needing the tree or the prose is a model seat
  bounded by code; nothing in between, because reasoning-shaped script guards
  ship defects.
- One adversarial checker verifies every claim against the tree (including
  read-only git, so a claim about a commit or a date is checked at source)
  and answers twice from one read: errors only, in chat register, never
  proposing wording; and a per-claim table (true / false / unverifiable) of
  every symbol, number, date and behavioural assertion. The loop is capped
  (two rounds for a paragraph, three for a file) and a loop still finding
  errors at the cap stops and reports.
- A rewrite's facts survive by claims-diff, not by a reading seat: the
  original's claims are tabled before writing, each with a verdict and a
  class (load-bearing, history git holds, or code-derivable), and only
  load-bearing claims must survive. Known-false claims are dropped by code;
  a claim evidenced solely by a run recorded as unreliable is never
  load-bearing. After the loop the survivors are matched mechanically against
  the rewrite, with one cheap aligned pass over the unmatched residue. That
  catches what the checker cannot: a paraphrase that is true and has still
  lost the specifics.
- One cold reader with no context and no tools reads top to bottom, once, and
  flags what it could not understand at the point it met it (a term defined
  only later still fails where it first appears). Its blindness is the
  instrument, and it does not fold into the sighted checker: seats predicting
  a human reader were measured and failed.
- A paragraph job gives the writer its neighbours and heading read-only; a
  paragraph that wins in isolation and loses in situ is the failure this
  prevents.
- Quality judgment of the result is the user's, blind, pairwise where
  possible, with sides counterbalanced, because single-sided reading order
  was measured to carry a position effect.

> **Finding:** (2026-09-21 to 2026-09-27, measured in the or3 repository where the
> pipeline was developed, against the blind-writer pipeline it replaced)
>
> A sighted writer under a register brief matched the blind writer on style (a
> dead heat under blind pairwise judging), made fewer false claims and fewer
> unverifiable ones, capped out the check loop once against four times, needed no
> explain seat, and wrote 74% less prose for the same jobs. Seats predicting a
> human reader agreed with held-out verdicts on 7 of 19 and 6 of 19. Single-sided
> reading order showed a position effect of 13 of 16. The runner's pins, `claude-opus-4-6`
> for the writer and `sonnet` for the checker, are the models measured.

## Splitting and merging

- The reader defines the subject: a file is one subject when a reader who needs
  part of it needs most of it. When real tasks read disjoint halves, it is two
  files, however well one summary covers both.
- The summary must discriminate, not abstract: written honestly, it is false of
  every other file in the corpus. "Everything to do with X" is a topic, not a
  subject; any file abstracts into a topic if enough information is dropped, so
  breadth of summary never argues against a split, and disjoint readers argue for
  one.
- Subjects split files; size never does. A long file with one subject stays
  whole.
- The name cap is the merging test: a subject whose name needs a fourth segment
  folds into its parent.

## When docs change

A pull request that changes a fact a page states changes the page in the same pull
request, through the pipeline. A change that bumps the version updates `README.md`.

## Staleness

- A stale doc is rewritten to current truth or deleted, never marked stale. A
  stale-marked doc keeps its authority and loses only its accuracy.
- A transient doc (a closed question) is deleted outright. Deleting a subject's
  file is proposed to the user first: git recovers a wrong deletion, but nothing
  surfaces a silently missing subject.

## Checks

`tests/test_docs.py` enforces the mechanical rules: the header shape, prose wrapped at
90 columns, no dash asides, no line-number citations, and every path and symbol the
prose names resolves.
