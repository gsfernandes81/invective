export const meta = {
  name: 'docs-pipeline',
  description: 'Consolidated docs pipeline: sighted writer, code gates first, one adversarial checker, claims-diff preservation, one blind cold reader',
  phases: [
    { title: 'Original claims', detail: 'a file rewrite, or a paragraph given old: claims table of the original, verdicts vs the tree' },
    { title: 'Write', detail: 'sighted opus-4.6 writer; literal-code gates before any downstream seat' },
    { title: 'Check', detail: 'one adversarial seat, errors + claims, looped' },
    { title: 'Preserve', detail: 'a file rewrite, or a paragraph given old: code claims-diff, alignment seat for residue' },
    { title: 'Cold read', detail: 'one blind reader; restatability is the one thing no other seat checks' },
  ],
}

// ---------------------------------------------------------------------------
// WHAT THIS IS. The docs pipeline described under "The pipeline" in
// docs/documentation.md, whose Finding block records the measurements behind
// the seats. Every seat here earned its place by owning a defect class nothing
// else catches:
//   writer (sighted)     -- the prose; blindness measured as not earning its
//                           seats (style dead heat, worse facts, worse cost)
//   code gates           -- process-talk leakage, header shape, 90-char wrap,
//                           size ceilings; predicates a unit test can evaluate
//                           NEVER become model seats
//   adversarial checker  -- falsehoods on the page (errors), and the per-claim
//                           existence table (claims), one tree read for both
//   claims-diff          -- load-bearing facts LOST in a rewrite; the checker
//                           structurally cannot see these (a paraphrase is
//                           true); known-false original claims are dropped by
//                           code, never judged
//   blind cold reader    -- restatability; a sighted seat self-reports this
//                           and panels measurably cannot predict the judge
// Landing stays a person's act: this runner NEVER writes docs/.
// ---------------------------------------------------------------------------

// args:
//   mode:    'paragraph' | 'file'
//   target:  docs/ path being written or rewritten
//   job:     paragraph mode -- one sentence: what the paragraph gives a reader
//   about:   paragraph mode -- files/symbols the paragraph is about
//   context: paragraph mode, optional -- neighbouring paragraphs + heading,
//            passed read-only to the writer (the judge required this twice)
//   old:     paragraph mode, optional -- the text being replaced, verbatim.
//            Shown to the writer as the text to rewrite, and its claims are
//            tabled and diffed like a file rewrite's, so a load-bearing fact
//            the new paragraph drops is caught. Splicing stays the caller's.
//   intent:  file mode -- why the file is being (re)written and what the
//            result must do; the caller's honest account, including any
//            premise they already know is false
//   thesis:  file mode, required -- one or two sentences naming what matters
//            most on the page. Brief input only: the writer is told to lead
//            with it. No seat checks it; position is the user's to judge on
//            the page.
//   rewrite: file mode -- true when target exists and its facts must survive
const mode = args.mode
const target = args.target
if (mode !== 'paragraph' && mode !== 'file') throw new Error("args.mode must be 'paragraph' or 'file'")
if (!target) throw new Error('args.target is required')
if (/^docs\/plans\//.test(target)) throw new Error(`${target} is exempt from the pipeline; write it directly`)
if (mode === 'paragraph' && (!args.job || !args.about)) throw new Error('paragraph mode needs args.job and args.about')
if (mode === 'paragraph' && args.old && /^\s*#/.test(args.old)) throw new Error('args.old starts with a heading: paragraph mode writes body text only; a heading or the header summary belongs to file mode or a direct edit')
if (mode === 'file' && !args.intent) throw new Error('file mode needs args.intent')
if (mode === 'file' && !args.thesis) throw new Error('file mode needs args.thesis: what matters most on the page, one or two sentences')
if (mode === 'file' && typeof args.rewrite !== 'boolean') throw new Error('file mode needs args.rewrite: true|false')

const WRITER = { model: 'claude-opus-4-6' }   // pinned by the Finding in docs/documentation.md
const CHECKER = { model: 'sonnet' }           // the measured instrument; raise
                                              // deliberately, not by drift
// Sighted seats (writer, checker, claims extractor) run as agentType
// 'sighted-seat': Read/Grep/Glob/Bash only, so no seat can message another
// session (first live runs, 2026-09-27: writer and checker seats did, to
// "coordinate"). Agent types register at session start, so a session older
// than .claude/agents/sighted-seat.md cannot resolve it; there the seat runs
// as a plain agent and the brief line below is the only fence. Said once.
// Prepended, never appended: every seat's prompt ends with the text under
// review, so a line added after it reads as that text's last sentence (it did,
// and a checker capped a run on it as an error).
const NO_PEERS = 'Coordination with other sessions is handled outside this run: do not look for other agents or message anyone.\n\n'
let seatFenced = true
async function seat(prompt, opts) {
  if (seatFenced) {
    try {
      return await agent(NO_PEERS + prompt, { ...opts, agentType: 'sighted-seat' })
    } catch (e) {
      if (!/agent type 'sighted-seat' not found/.test(String(e))) throw e
      seatFenced = false
      log("sighted-seat is not registered in this session (older than its definition): seats run unfenced, with the no-messaging line only")
    }
  }
  return await agent(NO_PEERS + prompt, opts)
}

const CAP = mode === 'file' ? 3 : 2
const REGISTER = 'Write it the way you would explain it aloud to a colleague, then tighten.'
const ANTI_TRANSCRIPTION = `
The reader has the code open: state only what they cannot derive from it, and
never paraphrase code. Read to VERIFY what you say, not to collect things to
say -- a paragraph that inventories the code has failed.`

// `notes` is where a seat's reports and departures go, so they never ride
// into the page; the runner hands them back to the caller untouched.
const DOC = { type: 'object', properties: { text: { type: 'string' }, notes: { type: 'string' } }, required: ['text'] }
const CHECK = {
  type: 'object',
  properties: {
    errors: { type: 'array', items: { type: 'string' } },
    blast: { type: 'array', items: { type: 'string' } },
    claims: { type: 'array', items: {
      type: 'object',
      properties: { claim: { type: 'string' }, verdict: { type: 'string', enum: ['true', 'false', 'unverifiable'] } },
      required: ['claim', 'verdict'] } },
  },
  // `blast` is findings about the paragraph's NEIGHBOURS. It goes back to the
  // caller and never into the revise loop: a correct paragraph beside a stale
  // one would otherwise loop to the cap on errors it cannot fix.
  required: ['errors', 'claims'],
}
// The drop taxonomy the first consolidation lost and the first ratification
// read paid for: only LOAD-BEARING claims must survive a rewrite. History git
// holds and code-derivable detail are correct drops -- reported, never
// enforced. A claim whose only evidence is a run the page itself marks
// unreliable can never classify load-bearing.
const CLAIMS_ONLY = {
  type: 'object',
  properties: { claims: { type: 'array', items: {
    type: 'object',
    properties: {
      claim: { type: 'string' },
      verdict: { type: 'string', enum: ['true', 'false', 'unverifiable'] },
      cls: { type: 'string', enum: ['load-bearing', 'history', 'derivable'] },
    },
    required: ['claim', 'verdict', 'cls'] } } },
  required: ['claims'],
}
const ALIGN = {
  type: 'object',
  properties: { judgments: { type: 'array', items: {
    type: 'object',
    properties: { claim: { type: 'string' }, present: { type: 'boolean' }, note: { type: 'string' } },
    required: ['claim', 'present'] } } },
  required: ['judgments'],
}
const COLD = { type: 'object', properties: {
  unrestatable: { type: 'array', items: { type: 'string' } },
  // paragraph mode: terms the page likely defines before this paragraph
  pageTerms: { type: 'array', items: { type: 'string' } } }, required: ['unrestatable'] }

// ---- literal-code gates. A predicate a unit test can evaluate is code; a
// predicate needing the tree or the prose is a model seat wrapped in code.
// Nothing in between: reasoning-shaped guards in script form are what three
// review rounds of the old pipeline kept shipping defects into.
// Phrases only a seat talking to its runner uses. Words a page may be ABOUT
// (the brief, the pipeline, upstream) are not here: docs/documentation.md is
// about the pipeline, and a pattern that matched them refused correct paragraphs.
const META_RE = /(i was given|the requesting agent|sighted pass|not one i can make|departures? (from|to) (the|my|this) brief|flag(ging)? (this|it) (upstream|for)|whoever (assembles|reviews) this|let me know (if|whether) you)/i
// The user may reverse the 90-char wrap decision; it is one number here.
const WRAP = 90
// stripped of inline code before the dash and citation tests
const stripCode = s => s.replace(/`[^`]*`/g, '')
// Identifiers the replaced text marked as code: a path, a dotted name, or a
// CONSTANT. Plain words in backticks are left out, since prose reuses them.
const codeIdents = s => [...new Set((s.match(/`[^`\n]+`/g) || []).map(c => c.slice(1, -1))
  .filter(c => /^[\w./:-]+(\(\))?$/.test(c) && (/[./]/.test(c) || /^[A-Z][A-Z0-9_]{2,}$/.test(c))))]
const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
function proseLines(t, check) {
  let fenced = false
  for (const [n, line] of t.split('\n').entries()) {
    if (/^```/.test(line)) { fenced = !fenced; continue }
    if (fenced || /^\s*\|/.test(line) || /^\s{4,}/.test(line)) continue
    const v = check(line, n + 1)
    if (v) return v
  }
  return null
}
function gate(text) {
  const t = (text || '').trim()
  if (!t) return 'empty draft'
  for (const para of t.split(/\n\n+/)) {
    if (META_RE.test(para)) return `process-talk in the deliverable: "${para.slice(0, 90)}..."`
  }
  const lineRules = (line, n) => {
    if (line.length > WRAP) return `prose line ${n} is ${line.length} chars; prose wraps at ${WRAP}`
    const bare = stripCode(line)
    if (/—|–| -- | - |--$|^--/.test(bare.replace(/^\s*[-*]\s+/, ''))) return `dash aside on line ${n}: asides sit in brackets, never dashes, whatever the neighbours use (code spans and tables exempt)`
    if (/\.(py|md|js|sh|toml|ya?ml|cfg):\d+|\blines? \d+/.test(bare)) return `line-number citation on line ${n}: cite the symbol, never the line`
    return null
  }
  if (mode === 'paragraph') {
    if (/^#/.test(t)) return 'paragraph job returned a heading; return paragraph text only'
    if (t.length > 3200) return `draft is ${t.length} chars; a paragraph past ~3200 is a section`
    const v = proseLines(t, lineRules)
    if (v) return v
    if (args.old) {
      const bare = stripCode(t)
      for (const id of codeIdents(args.old)) {
        if (new RegExp(`(^|[^\\w./-])${esc(id)}(?![\\w-])`).test(bare))
          return `\`${id}\` was code in the replaced text and appears here as plain prose; keep it in backticks`
      }
    }
    return null
  }
  const lines = t.split('\n')
  if (!/^# \S/.test(lines[0])) return 'line 1 must be "# <subject>"'
  if (lines[1] !== '') return 'line 2 must be blank (header: # subject, blank, <=2 summary lines, blank)'
  let i = 2, summary = 0
  while (i < lines.length && lines[i] !== '') { summary += 1; i += 1 }
  if (summary < 1 || summary > 2) return `header summary must be 1-2 lines (found ${summary}); head -4 is the index`
  return proseLines(t, lineRules)
}

// ---- stage: original claims (a file rewrite, or a paragraph given \`old\`) --
const preserving = (mode === 'file' && args.rewrite) || (mode === 'paragraph' && !!args.old)
let origClaims = []
if (preserving) {
  phase('Original claims')
  const orig = await seat(`
${mode === 'file' ? `Read \`${target}\` in full.` : `Below is a passage of \`${target}\` that is about to be replaced.`} List EVERY concrete claim it makes -- each
measurement with number and date, each symbol, path, command, config key,
each behavioural assertion, each stated open question. Atomic; err toward
more entries. For each, TWO judgments, from the tree AS IT IS TODAY:

\`verdict\`: "true", "false" (contradicted, or a measurement whose producing
run is recorded as unreliable), "unverifiable".

\`cls\`: "load-bearing" (a hazard, constraint, decision's why, open question,
or current mechanism a reader acts on), "history" (a dated event or instance
git already holds -- past bugs found, past campaign detail), "derivable"
(restatable from the code by a reader with it open). A claim whose only
evidence is a run the page itself marks unreliable is never "load-bearing".
Unsure between load-bearing and the others means "load-bearing".
File-reading tools and READ-ONLY git (log, show, blame, diff) only.${mode === 'paragraph' ? `

--- the passage being replaced ---
${args.old}` : ''}`,
    { ...CHECKER, phase: 'Original claims', label: 'orig-claims', schema: CLAIMS_ONLY })
  origClaims = orig?.claims || []
  if (!origClaims.length) throw new Error('original claims extraction returned nothing; a rewrite cannot run without its contract')
  log(`original: ${origClaims.length} claims, ${origClaims.filter(c => c.verdict === 'false').length} false (auto-drop), ${origClaims.filter(c => c.verdict === 'unverifiable').length} unverifiable`)
}

// ---- stage: write -----------------------------------------------------------
phase('Write')
const writerBrief = (prev, errs, violation) => mode === 'paragraph' ? `
You are writing ONE PARAGRAPH of documentation for this repository, which you
can read. Use file-reading tools (Read, Grep, Glob) and READ-ONLY git -- log, show,
blame, diff -- for history questions: commits, dates, what a change was.
Run nothing else, and nothing that writes.

The paragraph's job: ${args.job}
It is about: ${args.about}
${args.old ? `\n--- the text being replaced: yours to rewrite. It may be wrong; the job says what changes, and anything in it you keep must hold against the tree ---\n${args.old}\n` : ''}${args.context ? `\n--- the paragraph's neighbours and heading, read-only; write nothing outside your paragraph, contradict nothing here ---\n${args.context}\n` : ''}${ANTI_TRANSCRIPTION}
${violation ? `\nYour previous attempt was refused by a mechanical gate: ${violation}. Return only what the page's reader would see.\n` : ''}${prev ? `\n--- your previous draft ---\n${prev}\n` : ''}${errs ? `\n--- factual errors found in it; fix exactly these ---\n${errs.join('\n')}\n` : ''}
Asides sit in brackets, never dashes, even where the neighbours or the text you
replace use them: a gate refuses dashes. Prose wraps at ${WRAP} characters.
${REGISTER}

Return only the paragraph text. No heading, no preamble, nothing addressed to
the pipeline or its operators.` : `
You are writing ONE documentation file for this repository, which you can
read. Use file-reading tools (Read, Grep, Glob) and READ-ONLY git -- log, show,
blame, diff -- for history questions: commits, dates, what a change was.
Run nothing else, and nothing that writes.

The file: \`${target}\`.${args.rewrite ? ' Read it in full first; you are producing its complete replacement.' : ' It does not exist yet; you are writing it whole.'}

--- why, and what the result must do ---
${args.intent}

--- what matters most on this page ---
${args.thesis}
Lead with it; everything else supports it or comes after.
${ANTI_TRANSCRIPTION}
What git already holds -- dated instances, past campaign detail -- earns body
space only as a distilled lesson. An annex at the end may keep dated history
where the intent allows it, and deletion is often the correct edit: a page is
not a ledger, and a reference that is not earning its place for this page's
reader goes rather than gets explained.

Asides sit in brackets, never dashes, whatever the page used before: a gate
refuses them. Header shape: line 1 "# <subject>", blank line, at most two summary lines
(what the file holds and its state), blank line. Prose wraps at 90 characters;
tables and code blocks exempt. A measurement keeps its date; a changed premise
rewrites its paragraph, never an appended update.
${violation ? `\nYour previous attempt was refused by a mechanical gate: ${violation}. Return only the finished page.\n` : ''}${prev ? `\n--- your previous draft ---\n${prev}\n` : ''}${errs ? `\n--- factual errors found in it; fix exactly these ---\n${errs.join('\n')}\n` : ''}
${REGISTER}

Return the file's full text. Nothing addressed to the pipeline or its
operators.`

const notes = []
const wrote = r => { if (r?.notes) notes.push(r.notes); return r?.text }
let draft = wrote(await seat(writerBrief(null, null, null),
  { ...WRITER, phase: 'Write', label: 'write', schema: DOC })) || ''
let gateTrips = 0
{
  const v = gate(draft)
  if (v) {
    gateTrips += 1
    log(`gate: ${v}`)
    draft = wrote(await seat(writerBrief(draft, null, v),
      { ...WRITER, phase: 'Write', label: 'write-regate', schema: DOC })) || ''
    const v2 = gate(draft)
    if (v2) return { landed: false, failed: `gate twice: ${v2}`, gateTrips, notes }
  }
}

// ---- stage: check loop --------------------------------------------------------
phase('Check')
let rounds = 0, errors = [], claims = []
const blast = []
while (rounds < CAP) {
  rounds += 1
  const c = await seat(`
You are the one adversarial seat this ${mode === 'file' ? 'page' : 'paragraph'} gets.
${mode === 'paragraph' ? `It is about ${args.about} -- read those files first.` : `It is the candidate ${args.rewrite ? 'replacement' : 'text'} for \`${target}\`.`}
Answer two things from one read of the tree; file-reading tools and
READ-ONLY git (log, show, blame, diff) only.

FIRST, \`errors\`: verify every claim against the tree; ERRORS ONLY, one
string each, chat register, never propose wording. A measurement the text
dates and marks as made with a tool since found defective is correctly-marked
history, not an error; the same number presented as currently reliable IS one.
A number stated with its method is two claims: check that the method, done as
the text says, yields that number. Empty list means every claim held.

SECOND, \`claims\`: every concrete claim, atomic, each with a verdict "true" /
"false" / "unverifiable" (a marked historical measurement counts "true" as a
statement about what was measured then). Anything "false" belongs in
\`errors\` too.
${mode === 'paragraph' && (args.context || args.old) ? `
And two backstop questions. You see only the neighbours below, not the whole
page: the caller owns the page-wide search, so answer for what you can see and
claim no more.
  * THE BLAST RADIUS, into \`blast\`, never \`errors\`. Does this paragraph
    contradict the neighbours below, or is any neighbour already false against
    the tree? Quote the passage. The neighbours are not this paragraph's to
    fix, and may be rewritten by runs of their own. An empty list when you find
    nothing: never an entry saying a neighbour is fine.
  * THE PREMISE, into \`errors\`. Is anything the paragraph keeps from the text
    it replaces already false against the tree, for a reason the job does not
    name? A confident rewrite of a false premise reads better and is just as
    wrong.

${args.old ? `--- the text this paragraph replaces ---
${args.old}
` : ''}${args.context ? `--- the neighbours ---
${args.context}
` : ''}` : ''}
--- the text ---
${draft}`, { ...CHECKER, phase: 'Check', label: `check${rounds}`, schema: CHECK })
  errors = c?.errors || []
  claims = c?.claims || []
  for (const b of c?.blast || []) if (!blast.includes(b)) blast.push(b)
  log(`check ${rounds}: ${errors.length} errors, ${claims.length} claims`)
  if (!errors.length) break
  if (rounds >= CAP) break
  const again = wrote(await seat(writerBrief(draft, errors, null),
    { ...WRITER, phase: 'Check', label: `revise${rounds}`, schema: DOC }))
  if (again) {
    const gv = gate(again)
    if (!gv) { draft = again } else {
      // The revise may have fixed the checker's errors and tripped a gate on
      // form; keeping the older draft would re-send the fixed errors. Ask for
      // the same revision with the gate's complaint fixed, once.
      gateTrips += 1
      log(`revise gate: ${gv}`)
      const fixed = wrote(await seat(writerBrief(again, null, gv),
        { ...WRITER, phase: 'Check', label: `revise${rounds}-regate`, schema: DOC }))
      if (fixed && !gate(fixed)) draft = fixed
    }
  }
}
if (errors.length) {
  return { landed: false, stopped: `check loop hit the ${CAP}-round cap still finding errors`,
           rounds, errors, gateTrips, notes, blast, text: draft }
}

// ---- stage: preserve (rewrites only) -----------------------------------------
let preservation = null
if (preserving) {
  phase('Preserve')
  const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').split(/\s+/).filter(w => w.length > 2)
  const rewriteTokens = claims.map(c => new Set(norm(c.claim)))
  const draftNorm = new Set(norm(draft))
  const preserved = c => {
    const t = norm(c)
    if (!t.length) return true
    for (const rs of rewriteTokens) {
      if (t.filter(w => rs.has(w)).length / t.length >= 0.55) return true
    }
    return t.filter(w => draftNorm.has(w)).length / t.length >= 0.8
  }
  const mustSurvive = origClaims.filter(c => c.verdict === 'true' && c.cls === 'load-bearing')
  const residue = mustSurvive.filter(c => !preserved(c.claim))
  let losses = []
  if (residue.length) {
    const align = await agent(`
Below is a rewritten page, then claims its ORIGINAL made that a mechanical
match could not find in it. For each: is its SUBSTANCE present (paraphrase,
merged sentence, marked historical mention all count)? \`present\` true/false
with a short \`note\`.

--- the page ---
${draft}

--- claims to locate ---
${residue.map((c, i) => `${i + 1}. ${c.claim}`).join('\n')}`,
      { agentType: 'blind-writer', phase: 'Preserve', label: 'align', schema: ALIGN })
    losses = (align?.judgments || []).filter(j => !j.present)
  }
  preservation = {
    origClaims: origClaims.length,
    mustSurvive: mustSurvive.length,
    droppedFalse: origClaims.filter(c => c.verdict === 'false').length,
    allowedDropsHistory: origClaims.filter(c => c.verdict === 'true' && c.cls === 'history').length,
    allowedDropsDerivable: origClaims.filter(c => c.verdict === 'true' && c.cls === 'derivable').length,
    alignmentResidue: residue.length,
    losses,
  }
  log(`preserve: ${losses.length} candidate losses of ${mustSurvive.length} must-survive`)
}

// ---- stage: cold read ----------------------------------------------------------
phase('Cold read')
const cold = await agent(`
You have ZERO context for this project and cannot look anything up. Read the
text below as a stranger, TOP TO BOTTOM, ONCE. Restate ${mode === 'file' ? 'every heading and each paragraph\'s first sentence' : 'its first sentence and its main claim'} in your own
words. List in \`unrestatable\` everything you could not understand AT THE
POINT YOU MET IT -- a term of art defined only later in the page still counts
as a failure where it first appears. That is where the prose failed, not
where you did.${mode === 'paragraph' ? `

This is ONE paragraph from the middle of a longer page. A name the page would
plausibly have introduced before it (a system, a machine, a board, a project
term used as if already known) goes in \`pageTerms\`, not \`unrestatable\`:
the paragraph cannot define its page's vocabulary. Keep \`unrestatable\` for
what this paragraph itself fails to say clearly.` : ''}

--- the text ---
${draft}`, { agentType: 'blind-writer', phase: 'Cold read', label: 'cold', schema: COLD })

// Landing is a person's act: candidate losses and unrestatables go back
// through another invocation (they are writer input, not runner judgment),
// and doc-quality judgment stays the user's, blind.
return {
  landed: false,
  mode, target, gateTrips, rounds,
  chars: draft.length,
  claims: claims.length,
  claimsFalse: claims.filter(c => c.verdict === 'false').length,
  claimsUnverifiable: claims.filter(c => c.verdict === 'unverifiable').length,
  preservation,
  unrestatable: cold?.unrestatable || [],
  pageTerms: cold?.pageTerms || [],
  notes,
  blast,
  text: draft,
}
