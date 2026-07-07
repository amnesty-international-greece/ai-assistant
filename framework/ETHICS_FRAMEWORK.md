# Ethics Framework - Building a Human-Rights-Respecting AI Tool

**North star:** the most ethical AI tool Amnesty members have ever used - one that
embodies, rather than contradicts, Amnesty International's own analysis of AI harms.

This document takes Amnesty International's briefing **"Unlawful by Design:
Exposing the Human Rights Costs of Generative AI"** (Index: **POL 40/0996/2026**)
as the governing framework for this project's *final aim*. Not everything here is
a development-stage blocker. Treat the **Launch Requirements** as conditions for a
later international launch; treat the **Already-Aligned** items as design
invariants we must not regress.

---

## 1. What the briefing actually argues

Amnesty's thesis, in three lines:

1. Mainstream **standalone generative AI** (ChatGPT, Gemini, LLaMA, Stable
   Diffusion, etc.) is built on **unlawful, non-consensual web-scraping** - a
   "mass invasion of privacy *by design*" - and is therefore **incompatible with
   international human rights law (IHRL)**.
2. The harms run the whole **data-pipeline supply chain**: privacy, equality &
   non-discrimination (English/Western skew, racial/gender bias), freedom of
   expression (over-broad moderation, civic-space shrinkage), freedom of thought
   (automation bias, manipulation), plus **environmental** cost (data-centre
   water/energy, falling on Global Majority communities) and **labour** harms
   (data annotation).
3. Amnesty calls for **prohibition** of such systems, and - crucially for us -
   explicitly names the lawful alternative.

> **The single most important sentence for this project.** Amnesty's own briefing
> endorses **small language models (SLMs)**: *"smaller in scope and size… highly
> domain-specific and specialized… less resource intensive, more accurate, based
> on often local and industry-specific training data, and less prone to the
> problems associated with LLMs."* Our planned architecture (local Greek SLMs -
> Meltemi/Krikri - fine-tuned on Amnesty's own consensual corpus, with a
> deterministic non-AI core) is *literally the path Amnesty advocates*.

So our job is not to retrofit ethics onto a generative-AI product. It is to stay
on the right side of lines Amnesty itself drew - and to close the one real gap.

---

## 2. The one honest tension: our current model dependency

Today the platform calls **frontier LLM APIs** (Gemini for prototyping, Claude
for production) for content steps (minutes drafting, circular drafting,
classification). Those models are built on exactly the web-scraped data pipeline
the briefing condemns. **This is the project's central ethical debt.**

It is *mitigated* (we don't train on scraped data; the LLM is a feature inside a
workflow, not a standalone generative product; we feed it only the org's own
consensual data) but not *resolved*. Resolution = the **SLM migration** already
on the roadmap (ROADMAP §6): move content generation to **local, open,
Greek-specialized models** running on our own infrastructure. That migration is
not merely a performance/cost choice - **it is the ethical core of the project.**

---

## 3. Where we already align (design invariants - do not regress)

| Amnesty concern | How this project already answers it |
|---|---|
| Mass non-consensual data collection | We **train nothing on scraped data**; we process only the organisation's own internal documents, with members' awareness. |
| LLM over-reach / inaccuracy | A **deterministic, model-free core** does most work (e.g. `minutes_skeleton.py`, the protocol/archive logic). Models are confined to narrow, scoped tasks. |
| Automation bias / freedom of thought | **Human-in-the-loop is mandatory.** Minutes, circulars, and decisions are AI-*drafted* but **never auto-finalised** - the SecGen/board approve. The tool is a force-multiplier, not an oracle. |
| Silent erasure of voice | The skeleton-builder **flags off-topic speech, never deletes it.** Members' recorded words are preserved for audit. |
| English / Western linguistic dominance | The platform is **Greek-first** by construction (Greek templates, Greek τόνος handling, Greek SLMs). This is the *corrective* the briefing asks for, not the harm. |
| Opaque "black box" outputs | Uncertainty is marked (`[ΝΑ ΕΠΙΒΕΒΑΙΩΘΕΙ]`); the deterministic core is fully inspectable and unit-tested. |
| Resource intensity | Batch, local, CPU-friendly inference (faster-whisper int8; SLMs) over always-on frontier calls. |

---

## 4. The gaps - Launch Requirements (mapped to the briefing's "To Companies")

These are **not** dev-stage blockers. They are conditions to satisfy before an
international launch / before holding this up as exemplary. Each maps to a
recommendation Amnesty makes "TO COMPANIES."

| # | Amnesty recommendation (to companies) | Launch requirement for this project |
|---|---|---|
| L1 | Conduct human-rights due diligence across the lifecycle | Run and publish a **Human Rights Impact Assessment (HRIA)** of this tool before launch; repeat on major changes. |
| L2 | Provide full transparency on data/processing + supply-chain & environmental footprint | Ship a public **model & data card**: which models, what data they see, where they run, retention, and a measured **energy/footprint** estimate. |
| L3 | Establish accessible grievance + remedy mechanisms | Give members a clear, easy way to **contest or correct** any AI-produced record about them (esp. minutes) and to flag a harmful output. |
| L4 | Proactively identify and eliminate discriminatory bias; discontinue if unfixable | Define and run a **Greek-language bias/accuracy evaluation** on minutes/circular outputs (names, gender, dialect, marginalized-group references) with a documented pass bar. |
| L5 | Meaningful consultation with affected communities | **Consult the board and members** on the tool's design and use; obtain informed **consent** for meeting transcription before any recording. ✅ **Board consent for transcription + RTMS granted (2026-05-31).** Still pending: per-meeting verbal consent confirmation at recording start, and consultation with the wider membership for member-facing uses. |
| L6 | Local-language resourcing; counter Global-Majority neglect | Treat multilingual output (Greek-first, then translation) as an **inclusion feature**; never let quality degrade for non-English content. |
| L7 | Minimise environmental impact (data centres, water, energy) | Prefer **local/on-prem SLM inference**; measure and report; justify any remaining frontier-API use. |
| L8 | (Implied by §2) Lawful model provenance | Complete the **SLM migration** so no content step depends on a model built on unlawful web-scraping; document provenance of every model used. |

---

## 5. Development-stage posture (what we hold ourselves to *now*)

While building, before launch is on the table, we commit to the cheap, high-value
subset:

- **Data minimisation by default** - feed models only what a step needs; never
  send personal data to a third-party endpoint that the workflow doesn't require.
- **Human gate on every member-facing or governance output** - no auto-publish.
- **Preserve, don't erase** - flagging over deletion remains a hard rule.
- **Prefer deterministic code** to a model wherever a model isn't truly needed.
- **Keep the SLM migration on the roadmap as the ethical priority**, not a
  nice-to-have.
- **Record model usage honestly** in code/docs so the eventual model card is a
  description of reality, not an aspiration.

---

## 6. The opportunity

Amnesty's briefing is, in effect, a specification for a *good* AI tool: small,
local, domain-specific, consent-based, language-just, human-supervised,
auditable, low-footprint. This project can be a **working demonstration** that
"sophisticated data-intensive technologies… do not need to be built on harmful
practices" - Amnesty's own words. That is the pitch to Amnesty International: not
"we used AI," but "we built the kind of AI the movement asked for."

---

*Living document. Source: Amnesty International, "Unlawful by Design: Exposing the
Human Rights Costs of Generative AI," POL 40/0996/2026 (2026),
`framework/POL4009962026ENGLISH.pdf`. Revisit on each major capability change and
whenever Amnesty updates its AI guidance.*
