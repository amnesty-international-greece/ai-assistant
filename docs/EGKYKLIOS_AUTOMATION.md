# Automating the Γενική Εγκύκλιος Ενημέρωσης

Written straight after producing the April - September 2026 circular by hand, so
that the plan describes the work as it actually is rather than as the code
imagines it. Every gap below is one I hit while writing that document.

## 1. What producing one actually takes

The circular has two halves with different authors and different sources.

**Part A, the Board.** One narrative subsection per meeting, plus the General
Assembly and anything the Board did outside its meetings. The facts come from
the minutes and from the decisions, and the decisions carry most of the weight:
a reader wants to know what was resolved, not who said what.

**Part B, the Office.** The Director's briefings, re-filed. This is the part no
template can do: the briefings are chronological lists under three headings
(events, education and funding, finance and staffing), while the circular wants
events, institutional interventions, media, finance, education, and the
International Secretariat. A press conference in a briefing becomes two entries
in the circular, one under events and one under media. Deciding where each item
belongs is the same problem the minutes organiser already solves for agenda
items.

For this edition I read: three past circulars, three Director's briefings
(1 April to 11 September), the minutes of four meetings, and the sixteen
captured decisions of the last three meetings. Roughly 250 KB of source text
for a 29 KB document.

## 2. What the existing workflow assumes, and what is actually there

`EgkykliosGeneralWorkflow` has the right ten steps: gather sources, extract
briefing texts, extract meeting summaries, draft, render, notify, approve,
archive, send, publish. Its first step asks the database two questions. Today
both come back empty:

| Source the workflow looks for | Where it looks | Rows for Apr - Sep 2026 |
|---|---|---|
| Director's briefings | `director_briefings` table | **0** (table is empty) |
| Finalised minutes | `workflow_state`, completed `board_meeting_minutes` | **0** (2 rows, both awaiting approval) |

So the workflow would refuse to start, correctly, and the reason is not a bug in
it. The inputs it needs are never written:

* **Briefings are never ingested.** The four briefings of 2026 exist as PDFs in
  SharePoint and in the Secretary General's mailbox. `director_briefing_intake`
  exists as a workflow, but no briefing has ever produced a row.
* **Minutes never finish inside the platform.** The minutes of ΔΣ03 to ΔΣ07 were
  produced outside it. There is no completed minutes workflow in the database,
  so nothing can be summarised from one.
* **Decisions are only half captured.** `meeting_events` holds 16 decisions, for
  ΔΣ05, ΔΣ06 and ΔΣ07 only, because the Zoom sidebar started being used at ΔΣ05.
  The decisions of ΔΣ01 to ΔΣ04 exist only as prose inside PDF minutes.

## 3. The gaps, in the order they block automation

**G1. No structured record of what the Board decided.** This is the first one.
Part A is a retelling of decisions; without them as data, every edition starts
by re-reading PDFs. The architecture review's Phase 4 ledger is exactly this,
and the circular is its first consumer beyond the minutes.

**G2. Documents arrive as PDFs, and the text comes out badly.** Extracting the
Jan - Mar circular gave one word per line, because it was exported from Google
Docs. Usable, but only after cleanup. The fix is upstream: keep the Markdown or
Doc original beside the archived PDF, and extract with a layout-aware reader
when only a PDF exists.

**G3. Nothing notices a missing source.** ΔΣ04 has no Director's briefing, and
the General Assembly of 25 - 26 April has no minutes in the archive at all. Both
were found because a person went looking. A period has a known shape: so many
meetings, each with an invitation, minutes and a briefing, numbered in sequence.
Any automation must check that shape before drafting, and say what is missing.

**G4. The briefings carry material that must never reach members.** Named
resignations and the reasons for them, individual salaries, performance
concerns, a colleague's letter about her own pay. I removed all of it by
judgement. An automated draft will reproduce whatever it is given, so redaction
has to be an explicit step with written rules and a human gate, not a hope.

**G5. The period is not always a quarter.** This edition covers six months
because the previous one slipped. The workflow defaults to calendar quarters.
The period should be "from the end of the last published circular to a date the
Secretary General gives", read from the register.

**G6. The house style lives only in old PDFs.** The section folder has a prompt
but no examples. The style is specific: `[date] Title` headings in Part B,
bold on the thing that matters in each paragraph, past tense narrative, a table
of contents with anchors, a fixed pair of opening paragraphs quoted verbatim
from the Internal Regulations.

**G7. No verification pass.** My draft carries two explicit markers where I
could not verify something. That habit should be mechanical: every date, figure
and name in the output traceable to a source line, and anything unsupported
either dropped or marked.

## 4. Target pipeline

```
resolve period          from the register: last circular -> today
   |
collect sources         meetings, minutes, briefings, assembly documents
   |
completeness check  ----> HALT with a list: ΔΣ04 has no briefing,
   |                      the General Assembly has no minutes
extract + normalise     prefer Markdown original; layout-aware PDF fallback
   |
redact                  HR and personal data rules, with a diff for review
   |
draft part A            from decisions + minutes, one subsection per meeting
draft part B            from briefings, re-filed into the circular's sections
   |
style pass              against the last three circulars as examples
   |
verify                  every fact traced to a source; mark the rest
   |
render -> review gate -> archive (protocol number) -> Brevo -> Discord
```

Only two steps need judgement that cannot be delegated: the redaction review and
the final approval. Everything else is mechanical once the inputs exist.

## 5. Plan

Each phase ends with something usable on its own. Effort assumes the current
pace: part-time, one maintainer, meetings continuing.

### Phase A: give the pipeline its inputs (2 to 3 weeks)

Nothing else matters until the sources are in the database.

1. **Backfill the briefings.** Walk the 2026 archive folder, match
   `Εισηγητικό/Ενημερωτικό Διευθυντή - Συνεδρίαση ΔΣxx`, extract the text and the
   `A/A` serial, and write `director_briefings` rows with their protocol number,
   meeting reference and SharePoint link. One command, re-runnable.
2. **Ingest new briefings automatically.** The Director's briefing arrives by
   email before every meeting. `director_briefing_intake` already watches that
   mailbox; it must end by writing the row rather than only filing the PDF.
3. **Backfill the decisions of ΔΣ01 to ΔΣ04** from the archived minutes into the
   same shape the sidebar writes, so Part A can be built for any period of 2026.
4. **Store the last three circulars as Markdown** under the section folder, as
   the style reference the prompt needs.
5. **`egkyklios check <period>`**: prints the period's expected shape against
   what exists, names every gap, and exits non-zero. This is the command that
   would have told us about ΔΣ04's missing briefing in April rather than in
   October.

### Phase B: make the minutes land (depends on the minutes work already planned)

The circular's Part A is only as good as the minutes pipeline. Running ΔΣ06 and
ΔΣ07 through it to completion, so that finalised minutes and their decisions are
in the database, is the real prerequisite. No separate work for the circular.

Add the General Assembly as a first-class document type while doing it: it has
minutes, decisions and an attendance list like any meeting, and today it is
absent from the archive altogether.

### Phase C: drafting that can be trusted (3 to 4 weeks)

6. **Section reconciliation, not a fixed template.** The Director writes under
   whatever headings suit the period, and that is deliberate: the circular's
   structure follows his, it does not dictate it. The step is therefore:

   1. parse each briefing in the period into headings and the items beneath them;
   2. split compound headings into atomic topics, so
      "Εκδηλώσεις - Ακτιβισμός - Θεσμικές παρεμβάσεις" yields three;
   3. cluster those topics across the period's briefings into candidate sections;
   4. place each item, allowing one item in two sections where it genuinely
      belongs in both, as a press conference belongs to both events and media;
   5. label each section with the previous circular's wording when the cluster
      matches it, so members see continuity, and with the Director's own wording
      otherwise;
   6. order the sections as the last edition did, appending anything new beside
      its nearest relative.

   The Secretary General sees a one-page section map first: the sections, how
   many items each holds, anything placed twice, and anything the model could
   not place. Reordering happens there, before any prose is written. The
   existing prompt already asks for sections that reflect the briefings
   "οργανικά"; this gives that instruction something to work from.
7. **Redaction rules** in the section profile, as data: categories that never
   reach members (named staff matters, individual pay, performance, health,
   anything a member could not be told in a room). The step produces the draft
   plus a list of what it removed, for the Secretary General to check.
8. **Verification pass.** Same approach as the minutes verifier: every date,
   number and proper name in the draft must appear in a source; the rest is
   marked `[ΝΑ ΕΠΙΒΕΒΑΙΩΘΕΙ]` rather than published quietly.
9. **Style examples** wired into the prompt, with the two opening paragraphs
   quoted verbatim and the `[date] Title` convention enforced.

### Phase D: the full run (1 to 2 weeks)

10. Period resolution from the register, the review gate by email as today, then
    archive, Brevo to the members segment, Discord announcement.
11. One end-to-end rehearsal: regenerate this April - September edition from the
    sources and diff it against the hand-written one. Differences are either a
    bug or a judgement call worth encoding.

## 6. Decisions taken (2026-10-02)

1. **Cadence: quarterly, as the Regulations say.** The six-month edition was a
   consequence of doing it by hand over the summer, not a new norm. The
   quarterly scheduler job already exists; the period should still be computed
   from the last published circular rather than from the calendar, so that a
   late edition widens its window instead of losing the months in between.
2. **The Director keeps his own headings.** The circular adapts to the briefings,
   not the reverse, which is why step 6 above reconciles headings rather than
   imposing them. Flexibility is the requirement: merge what is the same, split
   what is compound, move an item that sits oddly, and keep the result readable.
3. **Redaction: strip it, keep the circular members-friendly.** Named staff
   matters, individual pay, performance and anything a member could not be told
   in a room stay out. The judgement applied by hand in this edition is the
   standard to encode.
4. **The General Assembly minutes are handled manually for now.** No workflow
   work; the ΓΣ01-2026 minutes exist and will be filed by the Secretary General.
   Treating the Assembly as a first-class meeting stays on the list for Phase B,
   but nothing waits on it.

## 7. Small things this edition exposed

* `2026_017` and `2026_029` exist as files in SharePoint but have no row in the
  register. `register audit` reports them as gaps; the archive step should write
  the row in the same transaction as the upload.
* Every archived circular is filed as `Γενική Εγκύλιος`, missing a κ. Worth
  fixing the filename convention before another year accumulates.
* The Director's briefings are serial-numbered (`A/A 18` to `21`), which is a
  better completeness check than dates. Capture the serial as a field.
* PDF extraction of Google Docs exports is one word per line. Keep the original
  format beside the PDF.

