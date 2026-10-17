---
name: write-doc
description: Write or rewrite a file under docs/ so a person reading it on a phone, without the code open, trusts it and understands the mechanism. Use whenever a session is about to write, rewrite, extend or shorten a doc under docs/, whenever work has just finished and something learned belongs in docs/, and whenever the user says "document this", "update the docs", "write it up" or names a docs/ file to change, even if they do not say "doc". Not for docs/plans/, which is written directly.
---

# Writing a doc

## Who is reading

The reader is a colleague who could open the code but mostly will not. They read
docs on a phone, in a 40-column terminal, and trust the doc to tell them what the
thing does and why. They open the code later, on a laptop, when they change it. So
the doc has to carry the mechanism on its own, in sentences that mean something with
every backtick removed, and it has to be true, because this reader will not check.
Name the symbol in parentheses so the laptop reader can jump to it; never let the
sentence depend on it. A term this reader may not know ("inode", "idempotent") is
replaced by the plain thing it means, or explained where it first appears.

Write it the way you would explain it to that colleague at the next desk, then cut.
Read `docs/documentation.md` once before you start; its Writing section is the
checklist you will apply at the end, and its examples are what good looks like here.

## The faults this corpus has had

Each of these has appeared in docs/ and been cut. Knowing what they look like and
why they fail the reader is more use than a rule, because the rule is what produces
them: a writer told "be concise" writes clipped aphorisms, and a writer told "cite
symbols" writes a function in prose.

**Mannered phrasing** substitutes flourish for direct statement. "A hardlink is a
window, not a wall." "The copy owns its bytes." Each sounds like a maxim and costs
the reader a second read to decode. It is also imprecise: the flourish drags in
meanings the writer did not choose. Say what you mean. "A hardlinked copy shares the
original's inode, so writing a mutant would edit the project's own file." When a
literal sentence is available, use it.

**Personified abstractions** make things that cannot act into the subject. "The
marker speaks for its copy." "The tree remembers its owner." "The history decides
what runs first." A marker, a tree and a history do not speak, remember or decide; a
reader on a phone cannot tell what actually happens. Name the actor: the engine, the
plugin, the reaper, the user. "Each copy carries a marker file naming the process
that made it, and the reaper removes the copy only when that process is dead." "The
engine tries the test that killed this mutant last time before it runs the rest."
Passive voice only when the actor is unknown or does not matter.

**Intensifiers** claim an emphasis the sentence has not earned. Actually, genuinely,
outright, specifically, deliberately, explicitly, literally, simply, entirely. A
sentence like this one is the pattern: "The plugin is deliberately and explicitly a
package of its own, specifically because pytest actually loads plugins before any
conftest, so whatever the plugin imports genuinely comes from wherever invective is
installed." Delete them all. If the sentence then reads as weak, the fact was weak.
"The plugin is a package of its own because pytest loads plugins before any
conftest.py. A package the plugin imported would come from wherever invective is
installed, not from the copy that holds the mutant."

**Code narration** restates a function in prose. "`tree.working_tree` reaps the dead
copies, makes a temporary directory, walks the project with an ignore function that
tests each name against `SKIPPED` and for a `pyvenv.cfg`, and copies what is left."
Every line is true, the phone reader learns nothing from it, and it drifts the first
time the function changes. Write the behaviour and the one thing a reader of the
function would not have guessed: "The copy is of the project as it stands, uncommitted
edits and untracked files included, minus the names in `tree.SKIPPED` and any
directory holding a `pyvenv.cfg`."

**Repetition across sections** states a fact or a rule in full wherever it applies.
A rule given in one section and given again, in full, in the section about its
exception leaves the reader hunting for the difference between the copies, and
finding none. A fact or rule lives under the one heading that owns it. A later
paragraph that needs it says so in a clause ("the skipped names above") and moves on.
Rewrites keep this fault longest, so look for it on purpose: any sentence you could
point back to instead.

**An opening that cannot stand alone.** "The engine hands it a file." As the first
sentence of a paragraph that is a phrase only a reader of the code can decode. The
first sentence of a paragraph is its claim and has to carry it; the rest qualify.
"The engine tells the plugin which tests to keep by writing their node ids, one a
line, to the file `INVECTIVE_SELECTION` names." A reader who stops after one sentence
was told less, never told wrong.

## Form, for a phone

Paragraphs of three to six lines, the point in the first sentence. An aside sits in
parentheses, which read on any screen; a mechanical check refuses dashes, so write
without them from the start. A comparison is a table of two or three narrow columns,
or a short list when a table would not fit in 40 columns. Prose wraps at 90 columns.

Three or more parallel items that each carry a qualifier are a list, one per line.
"The copy leaves out version control (`.git`, `.hg`, `.svn`), caches (`.tox`, `.nox`,
`__pycache__` and whatever pytest, mypy and ruff keep), `node_modules`, invective's
own `.invective`, and any directory holding a `pyvenv.cfg` (a virtual environment)"
is five kinds of thing with nested parentheses in one sentence, and on a phone it is
a wall.

Expect the rewrite to be shorter than what it replaces. It earns length only with a
mechanism, a hazard or a correction the old doc lacked; a paragraph that restates, a
clause that re-explains, an aside the reader did not need, all go. The reader
scrolls every line you keep.

## The procedure

1. **Name the reader and the task.** One line, for yourself: who opens this file and
   what they need to be able to do or understand afterwards. If you cannot name them,
   there is no doc to write.

2. **For a rewrite, list what must survive.** Read the old file and write ten lines
   or so in a scratch file outside the repository: its hazards, constraints,
   decisions with their reasons, measurements with their dates, and open questions. A
   rewrite loses these silently, because a paraphrase that is true can still drop the
   specifics. Each one ends up in the new doc or is named in the commit message as
   dropped, with the reason.

3. **Draft.** Lead with the one idea the rest supports. For every behavioural claim,
   look at the code it describes before you write it, in the tree this doc will land
   in, not an older checkout; the reader will not look. Cite the symbol in
   parentheses, never the line. Keep what the code cannot say: why it is shaped this
   way, what it must never do, what was measured and when (as a `**Finding:**` block,
   per "No history" in `CLAUDE.md`), what nobody knows yet, written as unknown.

4. **Review the draft against the Writing section of `docs/documentation.md`**, item
   by item, and fix what you find. The checklist meets a finished text here on
   purpose: held in mind while drafting, it deforms the sentences. Then cut: read
   once more for any sentence that repeats one above it, any list hiding in a
   sentence, and any paragraph the reader could lose without losing a fact. For
   `docs/isolation.md` and `docs/memory.md`, where a false sentence can make a reader
   trust a verdict the engine would not give, one reviewer reads the doc beside the
   diff for false claims only, once.

5. **Run the docs test**, `uv run pytest tests/test_docs.py`, which refuses a bad
   header, a line over 90 columns, a dash aside and a line-number citation, and
   whatever else it reports. Fix what it names.

6. **Commit** with what was wrong and what is true now, and the list from step 2 of
   anything dropped. The commit message is the record a future reader checks before
   reversing the change.
