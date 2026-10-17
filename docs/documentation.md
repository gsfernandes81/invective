# Writing rules

How prose here is written: the reader a doc serves, the faults to cut, how a doc gets
written, and the shape, names and headers of docs/; settled.

## Who a doc is for

A doc is read by a colleague who could open the code but mostly will not. They read it
on a phone, in a 40-column terminal, and trust it to say what the thing does and why.
They open the code later, on a laptop, when they change it. Before writing, name that
person and what they need to be able to do or understand afterwards. If you cannot,
there is no doc to write.

Two things follow. The doc has to carry the mechanism on its own, in sentences that
mean something with every backtick removed; a symbol in parentheses lets the laptop
reader jump to the code, and the sentence must not depend on it. And the doc has to be
true, because this reader will not check: every behavioural claim is looked up in the
code before it is written down.

## Writing

Write the way you would explain it to a colleague at the next desk, then cut. The
faults below are the ones this corpus has had. Each is shown rather than ruled against,
because rules produce them: a writer told to be concise writes aphorisms, and a writer
told to cite symbols writes a function in prose.

**Mannered phrasing.** Flourish in place of a direct statement. It sounds like a maxim
and costs a second read.

> Before: A hardlink is a window, not a wall.
>
> After: A hardlinked copy shares the original's inode, so writing a mutant would edit
> the project's own file.

**Personified abstractions.** The marker, the tree and the history cannot speak,
remember or decide, and a reader on a phone cannot tell what happens. Name the actor:
the engine, the plugin, the reaper, the user.

> Before: The marker speaks for its copy, and the reaper listens.
>
> After: Each copy carries a marker file naming the process that made it. The reaper
> (`tree.reap`) removes a copy only when that process is dead.

**Intensifiers.** Actually, genuinely, outright, specifically, deliberately,
explicitly, literally, simply, entirely. Each claims an emphasis the sentence has not
earned. Delete them; if the sentence then reads as weak, the fact was weak.

> Before: The plugin is deliberately and explicitly a package of its own, specifically
> because pytest actually loads plugins before any conftest, so whatever the plugin
> imports genuinely comes from wherever invective is installed.
>
> After: The plugin is a package of its own because pytest loads plugins before any
> conftest.py. A package the plugin imported would come from wherever invective is
> installed, not from the copy that holds the mutant.

**Code narration.** A function restated in prose. Every line is true, the phone reader
learns nothing, and it drifts the first time the function changes. Write the behaviour
and the one thing a reader of the function would not have guessed.

> Before: `tree.working_tree` reaps the dead copies, makes a temporary directory, walks
> the project with an ignore function that tests each name against `SKIPPED` and for a
> `pyvenv.cfg`, and copies what is left.
>
> After: The copy is of the project as it stands, uncommitted edits and untracked files
> included, minus the names in `tree.SKIPPED` and any directory holding a `pyvenv.cfg`.

**Repetition across sections.** A fact or rule stated in full wherever it applies
leaves the reader hunting for the difference between the copies. It lives under the
one heading that owns it; a later paragraph that needs it points back in a clause.

**A list hiding in a sentence.** Three or more parallel items that each carry a
qualifier are a list, one per line. Five kinds of thing with nested parentheses in one
sentence are a wall on a phone.

**An opening that cannot stand alone.** The first sentence of a paragraph is its
claim; the rest qualify it. A reader who stops after one sentence was told less, never
told wrong.

> Before: The engine hands it a file.
>
> After: The engine tells the plugin which tests to keep by writing their node ids, one
> a line, to the file `INVECTIVE_SELECTION` names.

What the code cannot say is what survives a cut: a hazard, a constraint, a decision
with its reason, a measurement with its date and machine, an open question written as
open. History, and anything a reader would get by opening the function, may go. Unsure
means it stays. A rewrite is normally shorter than what it replaces, and earns length
only with a mechanism, a hazard or a correction the old doc lacked.

A doc states what is true now. No "previously", no appended update or correction, no
"outdated" mark: a changed fact rewrites its paragraph, and git holds the history. A
date belongs to a measurement or an event, never to commentary (`git blame` dates every
sentence already), and it is spelled as a `**Finding:**` block, whose form is under "No
history" in `CLAUDE.md`.

A procedure speaks to the actor: second person, in order, common case first, stopping
where the actions stop. A warning stands at the step it applies to and names the
concrete failure. A known mistake is shown in its exact wrong form, with what it
silently does. An example is complete enough to run as written. A comparison is a table
of two or three narrow columns, or a short list when a table would not fit on a phone.
Cite the symbol, never the line number: `tree.SKIPPED` survives a reshuffle and greps,
and a line number is wrong by the next commit with nothing to notice.

Form: prose wraps at 90 columns (table rows and code blocks are exempt), and an aside
sits in parentheses. The test named under Checks refuses the rest of the mechanical
rules, so a reviewer need not.

## How a doc gets written

The session that did the work writes the doc, in the same sitting, with the changed
code still in context, following the `write-doc` skill
(`.claude/skills/write-doc/SKILL.md`). Nobody knows more about the change and nothing
is cheaper. The skill is the brief: who reads, the faults above with their examples,
then draft, then review the draft against this section, then the docs test, then a
commit message that names what was wrong, what is true now, and anything dropped from
the old file with the reason. `docs/plans/` is the exception, and a plan is written
directly. Any model may make a correction directly (a wrong fact, a broken reference, a
typo) without rewording the surrounding prose.

The brief is shaped by what is known about how models follow writing instructions.
Compliance falls steadily as simultaneous instructions are added (Harada et al., EMNLP
2025, ten models, one to ten instructions). A rule with its reason generalizes to cases
the rule did not name, and a bare rule does not (Anthropic, prompting best practices).
A prohibition that names the habit primes it (arXiv 2601.08070, a preprint on forbidden
words), so the brief states what to do and the test refuses the rest. Examples steer
tone more reliably than rules, and a wrong example in the brief is imitated, so the
examples here are the register to write in: a brief made of aphorisms produces
aphorisms. A checklist applied to a finished draft does not deform it the way one held
in mind while drafting does (Self-Refine, NeurIPS 2023).

Rewriting is not a campaign. A doc is rewritten when a task touches it and it fails
its reader, and deletion is often the right rewrite.

## Shape

- docs/ is flat, markdown only, one file per subject. Names do the grouping: a topic
  folder re-asks "which folder owns this?" at every write, and a nested name cited
  bare loses its context.
- `plans/` is the one subdirectory, and it marks a lifecycle, never a topic. It holds
  a design for a feature that is decided but unbuilt, moved to docs/ proper once
  built or deleted once dropped. There is no `handoff/`.
- Nothing but markdown lives under docs/. A build asset, probe or bundle a doc cites
  is linked where it lives.
- There is no index. The headers are the index: `head -4 docs/*.md`. An index file is
  a second spelling of every header, and it disagrees with them the first time a file
  changes without it.

## Names

- A file is named by its subject, broad to narrow, hyphenated:
  `isolation-signals.md`. A sorted listing groups families for free, and the bare
  name cited anywhere still says what it is about.
- Three segments is the cap. A name that wants a fourth is a doc that belongs inside
  its parent subject.
- A file is renamed the moment its name stops stating its subject. The rename carries
  every citation with it; `tests/test_docs.py` goes red on the paths missed.

## The header

- The first line is `# <subject>`, then a blank line, then one or two summary lines
  (what the file holds and its state: settled, open, planned, or a procedure), then a
  blank line. `head -4` then shows the header and never body text.
- The summary answers one question, "do I need this file for my task?", and does not
  restate the filename. Written honestly, it is false of every other file in docs/.
- Where the project uses a word in two senses, the summary says which it means and
  names where the other lives.

## Comments and docstrings

- A comment states what the code cannot: the constraint, the reason, the silent
  failure a check stands for. Never what the next line does, where code came from, or
  why a change was correct. That last is the writer talking to a reviewer, and it is
  noise the moment the change merges.
- A docstring states the contract, what a caller may rely on, never the
  implementation, which sits directly below it.
- A comment holds only current, certain facts. A fact gone uncertain moves to docs/
  and is stated there as uncertain; a dead one is deleted.
- The Writing section applies whole.

## Splitting and merging

- The reader defines the subject. A file is one subject when a reader who needs part
  of it needs most of it. When real tasks read disjoint halves, it is two files,
  however well one summary covers both.
- Subjects split files; size never does. A long file with one subject stays whole.
- The name cap is the merging test: a subject whose name needs a fourth segment folds
  into its parent.

## When docs change

A pull request that changes a fact a page states changes the page in the same pull
request, through the `write-doc` skill. A change that bumps the version updates
`README.md`.

## Staleness

- A stale doc is rewritten to current truth or deleted, never marked stale. A
  stale-marked doc keeps its authority and loses only its accuracy.
- A transient doc (a closed question) is deleted outright. Deleting a subject's file is
  proposed to the user first: git recovers a wrong deletion, but nothing surfaces a
  silently missing subject.

## Checks

`tests/test_docs.py` enforces the mechanical rules: the header shape, prose wrapped at
90 columns, no dash asides and no line-number citations.
