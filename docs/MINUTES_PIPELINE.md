# Board-Meeting Minutes Pipeline

How a Zoom board meeting becomes a formal Greek πρακτικό - the architecture, the
components built so far, and what remains. This is the in-code companion to the
design discussion in [`ROADMAP.md` §6](../ROADMAP.md).

## The shape of it

```
Zoom meeting (cloud recording, per-participant audio ON)
        │
        ├─ during: SecGen taps Discord buttons  ──►  meeting_events  (votes, agenda marks, presence)
        │
        ▼  recording.completed webhook
   download per-participant audio  ──►  data/recordings/{uuid}/  + manifest.json
        │
        ▼  faster-whisper per file (glossary-primed)        [SPIKE-GATED model step]
   {speaker, text, start, end} segments  (wall-clock, speaker = file owner)
        │
        ▼  build_minutes_skeleton(agenda, events, segments, roster)   [PURE, built]
   deterministic scaffold: presence - per-item time-bound segments - votes - off-topic flags
        │
        ▼  SLM tiers (third-person cleanup → formal synthesis)        [LATER]
   draft πρακτικά (JSON, board_minutes.md structure)
        │
        ▼  approval_and_share  ──►  Google Doc  ──►  SecGen / board correct + sign off   [HUMAN GATE]
   finalised minutes → archive + Βιβλίο Αποφάσεων
```

**Design principles** (see `framework/ETHICS_FRAMEWORK.md`): the deterministic
core does most of the work; models are confined to narrow, local, scoped tasks;
nothing a member said is ever silently dropped (off-topic is *flagged*); and a
human always approves before anything is final.

## Why not RTMS / Zoom's transcript

Zoom's post-meeting transcription doesn't support Greek and emits garbage. We
therefore ignore Zoom's transcript entirely and run **our own** ASR on the
audio. Audio survives cloud recording fine, so we never needed live interception
(RTMS) - post-meeting per-participant cloud-recording audio gives us speaker
separation for free, with no live infrastructure, no Developer Pack, no
streaming cost. (Full rationale: ROADMAP §6.7.)

## Components

| Component | File | Status | Notes |
|-----------|------|--------|-------|
| Mid-meeting event store | `src/core/meeting_events.py` (+ `meeting_events` table) | ✅ built | votes / agenda marks / presence / breaks / notes |
| Event capture CLI | `minutes events record/list` in `src/cli/commands.py` | ✅ built | interim capture surface |
| Recording webhook + fetch | `POST /webhooks/zoom/recording`, `ZoomClient.download_recording_assets`, `get_past_participants` | ✅ built | CRC handshake + signature verify; dumps assets + `manifest.json` |
| On-demand fetch CLI | `minutes fetch-recording <uuid>` in `src/cli/commands.py` | ✅ built | pull a recording without waiting on the webhook |
| Skeleton builder | `src/workflows/minutes_skeleton.py` | ✅ built | pure: agenda windowing, presence, votes, off-topic flagging |
| Transcription orchestration | `src/workflows/minutes_transcription.py` | ✅ built | `Transcriber` protocol, manifest→segments (wall-clock), `build_minutes_from_recording` |
| faster-whisper transcriber | `FasterWhisperTranscriber` (same file) | 🟡 lazy stub | concrete ASR; exercised post-spike with the dep + real audio |
| **Pipeline orchestrator + config** | `src/workflows/minutes_pipeline.py`, `minutes build` CLI, `settings.minutes_pipeline` | ✅ built | one command: transcript-file (no-ASR, testable now) or manifest (audio) → skeleton → optional `--draft`; transcriber/model selectable in `config.yaml` |
| Governance ingestion (Καταστατικό) | `scripts/ingest_governance_docs.py` → `assets/governance/articles.json` | ✅ built | 28 Καταστατικό articles for decision grounding (Κανονισμοί = follow-up) |
| Decision drafter | `src/workflows/decision_drafter.py`, `minutes propose-decision` | ✅ built | deterministic ΔΣNN-MM-YYYY ref + grounded «έχοντας υπόψη»; anti-hallucination prompt |
| Discord mid-meeting control panel | - | ⬜ designed | buttons → `meeting_events`; awaits build with SecGen input |
| SLM cleanup + synthesis tiers | - | ⬜ designed | Tier-1 Krikri third-person; Tier-2 formal synthesis |
| `draft_minutes` rewire | `src/workflows/board_meeting_minutes.py` | ⬜ pending | swap garbage transcript for our skeleton-fed draft (after quality validation) |

## The wall-clock alignment (why it's spike-robust)

Each per-participant audio file carries its own `recording_start` (ISO, in the
manifest). Whisper returns segment offsets *relative to that file*. So:

```
segment.start_wallclock = file.recording_start + timedelta(seconds=offset)
```

This holds **whether** Zoom pads every file to meeting-start **or** starts each
file at the participant's join time - because each file's offsets are always
relative to its own `recording_start`. The spike only needs to confirm
`recording_start` semantics; the formula doesn't change.

## What the spike still decides

One short test recording (2-3 people, each on their own connection, speaking
Greek), captured by the webhook, then inspect `data/recordings/{uuid}/manifest.json`:

1. **Where does per-participant audio live** - `recording_files` or
   `participant_audio_files`? (We capture both, so this just confirms.)
2. **`recording_start` semantics** - common meeting-start vs per-join offset.
3. **Greek quality** - run `FasterWhisperTranscriber` on one file with a name
   glossary; confirm accuracy is good enough to feed the SLM tier.

## Running the pieces today

```bash
# Record events during a meeting (interim, before the Discord panel exists):
python -m src.cli minutes events record --meeting-ref ΔΣ05-2026 --type vote \
  --payload '{"label":"Έγκριση προϋπολογισμού","result":"passed","tally":{"υπέρ":4,"κατά":1,"αποχή":0},"method":"majority"}'
python -m src.cli minutes events list --meeting-ref ΔΣ05-2026

# Pull a recording's assets on demand (after enabling per-participant audio):
python -m src.cli minutes fetch-recording <meeting_uuid> --participants

# Run the whole pipeline. TEST TODAY with a pasted transcript (no Zoom/Whisper):
python -m src.cli minutes build ΔΣ05-2026 --transcript-file <transcript.vtt|txt> \
  --meeting-start 2026-06-15T18:00:00+00:00 [--draft]
# …or from a real recording's per-participant audio:
python -m src.cli minutes build ΔΣ05-2026 --manifest data/recordings/<uuid>/manifest.json [--draft]

# Propose a formal decision (computed ΔΣNN-MM-YYYY ref + grounded «έχοντας υπόψη»):
python -m src.cli minutes propose-decision --meeting-ref ΔΣ05-2026 --snippet "<discussion>"
```

Pipeline behaviour is configured in `config.yaml → minutes_pipeline`
(`transcriber: faster_whisper|fake`, `whisper_model`, `language`, paths).

The webhook (`POST /webhooks/zoom/recording`) does the fetch automatically on
`recording.completed` once the Zoom app's webhook URL + `ZOOM_WEBHOOK_SECRET_TOKEN`
are configured.

## Build order remaining

1. **Spike** (above) - confirm manifest reality. *Unblocked; needs one test recording.*
2. **Wire faster-whisper** - install the dep, plug `FasterWhisperTranscriber`
   into `build_minutes_from_recording`, verify Greek on real audio.
3. **Discord control panel** - buttons → `meeting_events` (with SecGen input on
   the button set).
4. **SLM tiers** - Krikri third-person cleanup → formal synthesis; LoRA
   fine-tune on past minutes for house register.
5. **Rewire `draft_minutes`** - consume the skeleton instead of Zoom's transcript.
6. **Multilingual output** - the Amnesty-International-facing stretch goal.
