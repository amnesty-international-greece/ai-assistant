"""Zoom In-Meeting App - FastAPI router.

Serves the sidebar web app that runs inside the Zoom desktop client during
board meetings, plus small JSON endpoints the sidebar calls for the agenda,
timeline events, and decisions.

The Home URL (``/zoom-app``) must respond with the required OWASP security
headers so Zoom's portal can validate it.

Zoom Apps SDK reference: https://appssdk.zoom.us
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["zoom-app"])

# Security headers Zoom's validation checker requires (OWASP baseline).
# frame-ancestors must include *.zoom.us so the Zoom client can embed this page.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self' https://*.zoom.us https://appssdk.zoom.us; "
        "script-src 'self' 'unsafe-inline' https://appssdk.zoom.us; "
        "style-src 'self' 'unsafe-inline'; "
        "frame-ancestors https://*.zoom.us"
    ),
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "X-XSS-Protection": "1; mode=block",
}


def _lookup_agenda(meeting_ref: str) -> tuple[str, list[str]]:
    """Return ``(meeting_ref, agenda_items)`` for the most recent invitation
    workflow matching ``meeting_ref`` (or the most recent one with an agenda
    if ``meeting_ref`` is empty).  Reads from ``workflow_state`` - no Google
    API call needed.
    """
    target = (meeting_ref or "").strip()
    try:
        from src.core.audit import _get_connection
        conn = _get_connection()
        rows = conn.execute(
            "SELECT data FROM workflow_state "
            "WHERE workflow_name = 'board_meeting_invitation' "
            "ORDER BY updated_at DESC"
        ).fetchall()
        for row in rows:
            try:
                ctx = (json.loads(row["data"] or "{}")).get("context", {})
            except (json.JSONDecodeError, TypeError):
                continue
            ref_row = (ctx.get("raw_meeting_id") or ctx.get("meeting_ref") or "").strip()
            if target and ref_row != target:
                continue
            items = ctx.get("agenda_items") or []
            if items:
                return (ref_row or target, list(items))
    except Exception as e:
        logger.warning("zoom-app agenda lookup failed: %s", e)
    return (target, [])


@router.get("/zoom-app/agenda")
async def zoom_app_agenda(ref: str = ""):
    """Agenda items for the current meeting, called by the sidebar via fetch."""
    meeting_ref, items = _lookup_agenda(ref)
    return JSONResponse({"meeting_ref": meeting_ref, "items": items})


# ── Timeline events (agenda markers, session phases) ──────────────────────────

class EventIn(BaseModel):
    meeting_ref: str
    event_type: str            # "agenda_advance" | "phase" | "off_topic" | ...
    payload: dict = {}


@router.post("/zoom-app/event")
async def zoom_app_record_event(body: EventIn):
    """Record any timestamped in-meeting event (agenda advance, session phase).

    These build the timeline that maps *when* each agenda item was discussed,
    for later sync with the Zoom per-participant audio tracks → agenda-split
    Whisper transcripts.  Timestamp is server-side ``now`` at receipt.
    """
    from src.core.meeting_events import MeetingEventsStore
    meeting_ref = body.meeting_ref.strip()
    if not meeting_ref:
        return JSONResponse({"ok": False, "error": "meeting_ref required"}, status_code=400)
    try:
        eid = MeetingEventsStore().record_event(
            meeting_ref=meeting_ref, event_type=body.event_type, payload=body.payload,
        )
    except ValueError as e:   # invalid event_type
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("zoom-app record event failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "id": eid})


# ── Decision capture ──────────────────────────────────────────────────────────

class DecisionIn(BaseModel):
    meeting_ref: str
    decision_text: str
    outcome: str = ""               # "Έγκριση" | "Απόρριψη"
    considerations: list[str] = []   # «έχοντας υπόψη» lines
    agenda_index: int | None = None
    agenda_item: str = ""
    pre_meeting: bool = False        # decided by email BEFORE the meeting started


@router.get("/zoom-app/decisions")
async def zoom_app_list_decisions(ref: str = ""):
    """Decisions recorded so far for the meeting (running list + next sequence).

    Read-only - also used by the participant view.
    """
    meeting_ref = (ref or "").strip()
    if not meeting_ref:
        meeting_ref, _ = _lookup_agenda("")
    decisions: list[dict] = []
    if meeting_ref:
        try:
            from src.core.meeting_events import MeetingEventsStore
            evs = MeetingEventsStore().list_events(meeting_ref, event_type="decision")
            decisions = [e["payload"] for e in evs]
        except Exception as e:
            logger.warning("zoom-app list decisions failed: %s", e)
    return JSONResponse(
        {"meeting_ref": meeting_ref, "decisions": decisions, "next_seq": len(decisions) + 1}
    )


@router.post("/zoom-app/decision")
async def zoom_app_record_decision(body: DecisionIn):
    """Record a board decision from the sidebar into the meeting-events store.

    Sequence + canonical ref (``ΔΣNN-MM-YYYY``) are computed from how many
    decisions already exist for this meeting.
    """
    from src.core.meeting_events import MeetingEventsStore
    from src.workflows.decision_drafter import compute_decision_ref

    meeting_ref = body.meeting_ref.strip()
    text = body.decision_text.strip()
    if not meeting_ref or not text:
        return JSONResponse({"ok": False, "error": "meeting_ref and decision_text required"}, status_code=400)

    store = MeetingEventsStore()
    seq = len(store.list_events(meeting_ref, event_type="decision")) + 1
    try:
        ref = compute_decision_ref(meeting_ref, seq)
    except Exception:
        ref = f"{meeting_ref}-{seq:02d}"   # defensive fallback for odd refs

    payload = {
        "ref": ref,
        "seq": seq,
        "decision_text": text,
        "outcome": body.outcome.strip(),
        "considerations": [c.strip() for c in body.considerations if c and c.strip()],
        "agenda_index": body.agenda_index,
        "agenda_item": body.agenda_item.strip(),
        "pre_meeting": bool(body.pre_meeting),   # email decision before the meeting
    }
    try:
        store.record_event(meeting_ref=meeting_ref, event_type="decision", payload=payload)
    except Exception as e:
        logger.warning("zoom-app record decision failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "ref": ref, "decision": payload})


@router.api_route("/zoom-app", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def zoom_app_home():
    """Home URL - loaded inside the Zoom desktop client sidebar."""
    return HTMLResponse(content=_HOME_HTML, headers=_SECURITY_HEADERS)


_HOME_HTML = """<!DOCTYPE html>
<html lang="el">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Assistant - Διοικητικό Συμβούλιο</title>
  <script src="https://appssdk.zoom.us/sdk.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
      background: #f5f3ee; color: #0a0a0a; padding: 14px; font-size: 14px;
    }
    .header {
      background: #0a0a0a; color: #FFFF00; padding: 12px 14px; margin: -14px -14px 14px;
      font-weight: 700; font-size: 13px; letter-spacing: 0.1em; text-transform: uppercase;
    }
    .status { font-size: 12px; color: #888; margin-bottom: 12px; min-height: 16px; }
    .status.ready { color: #0a0a0a; }
    #rec-status {
      font-size: 12px; font-weight: 700; letter-spacing: 0.04em;
      padding: 8px 10px; margin-bottom: 12px; display: none;
    }
    #rec-status.rec    { display: block; background: #fdecea; color: #E63B11; }
    #rec-status.rec::before    { content: "● "; }
    #rec-status.paused { display: block; background: #fff7e0; color: #9a6b00; }
    #rec-status.paused::before { content: "❚❚ "; }
    #rec-status.ended  { display: block; background: #eee; color: #555; }
    #rec-status.ended::before  { content: "■ "; }

    .sec-title {
      font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase;
      font-weight: 700; color: #888; margin: 0 0 8px;
    }
    ol.agenda { list-style: none; margin: 0 0 12px; }
    ol.agenda li {
      display: flex; gap: 8px; align-items: baseline;
      padding: 8px 10px; border: 1px solid #e3e0d8; border-bottom: none;
      background: #fff; font-size: 13px; line-height: 1.35;
    }
    ol.agenda li:last-child { border-bottom: 1px solid #e3e0d8; }
    ol.agenda li.current { background: #FFFF00; border-color: #0a0a0a; font-weight: 600; }
    ol.agenda li.empty { color: #999; }
    body.live ol.agenda li { cursor: pointer; }
    body.live ol.agenda li:hover:not(.current) { background: #faf8f2; }
    ol.agenda li .n {
      flex: 0 0 20px; height: 20px; border-radius: 50%; background: #0a0a0a; color: #FFFF00;
      font-size: 11px; font-weight: 700; display: inline-flex; align-items: center; justify-content: center;
    }

    .controls { display: flex; gap: 8px; margin-bottom: 8px; }
    button {
      flex: 1; padding: 11px 10px; border: none; cursor: pointer;
      font-weight: 700; font-size: 13px; letter-spacing: 0.04em; text-transform: uppercase;
      background: #0a0a0a; color: #FFFF00;
    }
    button.alt { background: #FFFF00; color: #0a0a0a; border: 1px solid #0a0a0a; }
    button:disabled { opacity: 0.35; cursor: default; }
    button.ghost { background: transparent; color: #777; border: 1px solid #ddd; font-weight: 600; }
    button.danger { background: #E63B11; color: #fff; }
    /* Nav buttons: elegant edge-pinned arrows, labels never truncate */
    #nav button { position: relative; white-space: nowrap; padding: 11px 18px; }
    #nav .arw {
      position: absolute; top: 50%; transform: translateY(-50%);
      font-size: 17px; font-weight: 400; line-height: 1;
    }
    #prev .arw { left: 9px; }
    #next .arw { right: 9px; }

    /* Decision capture */
    hr.sep { border: none; border-top: 1px solid #e3e0d8; margin: 16px 0 14px; }
    textarea {
      width: 100%; resize: vertical; padding: 9px 10px;
      border: 1px solid #cfcabd; font-family: inherit; font-size: 13px; line-height: 1.4;
      margin-bottom: 8px;
    }
    #dec-cons { min-height: 44px; }
    #dec-text { min-height: 52px; }
    .outcome { display: flex; gap: 8px; margin-bottom: 8px; }
    .outcome button {
      border: 1px solid #0a0a0a; background: #fff; color: #0a0a0a;
      font-size: 12px; text-transform: none;
    }
    .outcome button.sel[data-oc="Έγκριση"] { background: #FFFF00; color: #0a0a0a; }
    .outcome button.sel[data-oc="Απόρριψη"] { background: #E63B11; color: #fff; border-color: #E63B11; }
    ol.declist { list-style: none; margin: 6px 0 0; }
    ol.declist li {
      padding: 7px 0; border-bottom: 1px solid #eee; font-size: 12px;
      display: flex; gap: 8px; align-items: baseline;
    }
    ol.declist li .ref { font-weight: 700; white-space: nowrap; }
    ol.declist li .oc-tag { margin-left: auto; font-size: 11px; font-weight: 700; white-space: nowrap; }
    ol.declist li .oc-tag.yes { color: #0a0a0a; background: #FFFF00; padding: 1px 5px; }
    ol.declist li .oc-tag.no { color: #E63B11; }
  </style>
</head>
<body>
  <div class="header">AI Assistant - ΔΣ</div>
  <div class="status" id="status">Σύνδεση με Zoom…</div>

  <div id="panel" style="display:none;">
    <div id="rec-status"></div>

    <p class="sec-title" id="agenda-label">Ημερήσια Διάταξη</p>
    <ol class="agenda" id="agenda"></ol>

    <!-- Before the official start: a single marker button -->
    <div id="ctrl-before">
      <button id="start">ΕΝΑΡΞΗ ΣΥΝΕΔΡΙΑΣΗΣ</button>
    </div>

    <!-- After start: navigation + session controls -->
    <div id="ctrl-live" style="display:none;">
      <div class="controls" id="nav">
        <button class="alt" id="prev"><span class="arw">‹</span>ΠΡΟΗΓΟΥΜΕΝΟ</button>
        <button id="next">ΕΠΟΜΕΝΟ<span class="arw">›</span></button>
      </div>
      <div class="controls">
        <button class="ghost" id="offagenda">ΕΚΤΟΣ ΗΜΕΡΗΣΙΑΣ</button>
        <button class="ghost" id="end">ΛΗΞΗ ΣΥΝΕΔΡΙΑΣΗΣ</button>
      </div>
    </div>

    <hr class="sep" />
    <p class="sec-title" id="dec-label">Καταγραφή Απόφασης</p>
    <textarea id="dec-cons" placeholder="Έχοντας υπόψη… (ένα στοιχείο ανά γραμμή)"></textarea>
    <textarea id="dec-text" placeholder="Κείμενο απόφασης…"></textarea>
    <div class="outcome">
      <button class="oc" data-oc="Έγκριση">ΕΓΚΡΙΣΗ</button>
      <button class="oc" data-oc="Απόρριψη">ΑΠΟΡΡΙΨΗ</button>
    </div>
    <button id="dec-save">✓ ΚΑΤΑΓΡΑΦΗ &amp; ΑΝΑΚΟΙΝΩΣΗ</button>

    <p class="sec-title" id="declist-label" style="margin-top:16px;">Καταγραφές</p>
    <ol class="declist" id="declist"></ol>
  </div>

  <script>
    const KEY = "agenda-marker";
    let AGENDA = [];
    let idx = -1;
    let offAgenda = false;
    let screenName = "";
    let MEETING_REF = "";
    let selectedOutcome = "";
    let recState = "before";   // before | live | ended
    let baseStatus = "";

    const $ = (id) => document.getElementById(id);

    function setStatus(text, ready) {
      const el = $("status");
      el.textContent = text;
      el.className = ready ? "status ready" : "status";
    }
    let toastTimer = null;
    function toast(text) {
      setStatus(text, true);
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => setStatus(baseStatus, true), 3000);
    }

    async function recordEvent(type, payload) {
      try {
        await fetch("/zoom-app/event", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ meeting_ref: MEETING_REF, event_type: type, payload: payload || {} }),
        });
      } catch (_) {}
    }

    // ── Recording status indicator (driven by actual recording state) ─────
    function applyRecStatus(state) {
      // state: 'started' | 'resumed' | 'paused' | 'stopped'
      const el = $("rec-status");
      if (state === "paused")      { el.className = "paused"; el.textContent = "ΣΕ ΠΑΥΣΗ"; }
      else if (state === "stopped"){ el.className = "ended";  el.textContent = "Η ΕΓΓΡΑΦΗ ΣΤΑΜΑΤΗΣΕ"; }
      else                         { el.className = "rec";    el.textContent = "ΕΓΓΡΑΦΗ ΕΝΕΡΓΗ"; }
    }

    // ── Agenda rendering ──────────────────────────────────────────────────
    function renderAgenda() {
      const ol = $("agenda");
      ol.innerHTML = "";
      if (!AGENDA.length) {
        ol.innerHTML = '<li class="empty">Δεν βρέθηκε ημερήσια διάταξη.</li>';
        return;
      }
      AGENDA.forEach((item, i) => {
        const li = document.createElement("li");
        if (i === idx && !offAgenda && recState === "live") li.className = "current";
        li.innerHTML = '<span class="n">' + (i + 1) + '</span><span>' + item + '</span>';
        li.onclick = () => { if (recState === "live") goTo(i); };
        ol.appendChild(li);
      });
    }

    // ── On-screen agenda banner (best-effort, never blocks navigation) ────
    async function setMarker(fullText) {
      const lengths = [fullText.length, 40, 30, 24, 18, 12];
      for (const n of lengths) {
        const t = fullText.length > n ? fullText.slice(0, n - 1) + "…" : fullText;
        try {
          await zoomSdk.setDynamicIndicator({ text: t, screenName: screenName, position: "topCenter", key: KEY });
          return true;
        } catch (e) {
          if (!/character limit/i.test(e.message || "")) return false;  // timeout/other → silent
        }
      }
      return false;
    }
    async function showMarker(i) {
      const ok = await setMarker((i + 1) + ". " + AGENDA[i]);
      if (!ok) return;
      const Y = "#FFFF00", B = "#000000";
      const styles = [
        { key: KEY, backgroundColor: Y, textColor: B, color: B, fontColor: B, foregroundColor: B },
        { key: KEY, backgroundColor: Y, textColor: B },
        { key: KEY, backgroundColor: Y, color: B },
        { key: KEY, backgroundColor: Y, fontColor: B },
        { key: KEY, backgroundColor: Y },
      ];
      for (const s of styles) { try { await zoomSdk.setDynamicIndicatorStyle(s); break; } catch (_) {} }
      try { await zoomSdk.sendMessageToChat({ message: "📋 Θέμα " + (i + 1) + ". " + AGENDA[i] }); } catch (_) {}
    }

    // ── Navigation (instant; marker + event fire async) ───────────────────
    function goTo(i) {
      if (recState !== "live" || i < 0 || i >= AGENDA.length) return;
      idx = i; offAgenda = false;
      renderAgenda(); updateControls();
      recordEvent("agenda_advance", { index: i, item: AGENDA[i] });
      showMarker(i);
    }

    function updateControls() {
      const live = recState === "live";
      document.body.classList.toggle("live", live);
      $("ctrl-before").style.display = (recState === "before") ? "" : "none";
      $("ctrl-live").style.display = live ? "" : "none";
      if (live) {
        $("prev").disabled = (idx <= 0);
        $("next").disabled = (idx >= AGENDA.length - 1);
      }
      updateDecLabel();
    }

    function updateDecLabel() {
      if (recState === "before") {
        $("dec-label").textContent = "Αποφάσεις δια email (πριν τη συνεδρίαση)";
      } else if (offAgenda || idx < 0) {
        $("dec-label").textContent = "Καταγραφή Απόφασης";
      } else {
        $("dec-label").textContent = "Καταγραφή Απόφασης - Θέμα " + (idx + 1);
      }
    }

    // ── Session controls ──────────────────────────────────────────────────
    $("start").onclick = () => {
      recState = "live";
      recordEvent("phase", { phase: "start", source: "app" });
      goTo(0);             // moves to first item, enables nav, records agenda_advance
    };
    $("prev").onclick = () => goTo(idx - 1);
    $("next").onclick = () => goTo(idx + 1);

    $("offagenda").onclick = () => {
      offAgenda = true;
      try { zoomSdk.removeDynamicIndicator({ key: KEY }); } catch (_) {}
      recordEvent("off_topic", { after_index: idx });
      renderAgenda(); updateDecLabel();   // clears the highlight
      toast("Εκτός ημερήσιας διάταξης - η εγγραφή συνεχίζεται.");
    };

    // ΛΗΞΗ - requires a second confirming click within 4s.
    let endArmed = false, endTimer = null;
    $("end").onclick = async () => {
      if (!endArmed) {
        endArmed = true;
        $("end").textContent = "ΕΠΙΒΕΒΑΙΩΣΗ ΛΗΞΗΣ;";
        $("end").className = "danger";
        endTimer = setTimeout(() => {
          endArmed = false; $("end").textContent = "ΛΗΞΗ ΣΥΝΕΔΡΙΑΣΗΣ"; $("end").className = "ghost";
        }, 4000);
        return;
      }
      clearTimeout(endTimer); endArmed = false;
      try { await zoomSdk.cloudRecording({ action: "stop" }); } catch (_) {}
      recState = "ended";
      recordEvent("phase", { phase: "end", source: "app" });
      try { await zoomSdk.removeDynamicIndicator({ key: KEY }); } catch (_) {}
      applyRecStatus("stopped");
      updateControls();
    };

    // Authoritative recording log - captures our stop AND manual native toggles.
    let recListenerSet = false;
    function ensureRecListener() {
      if (recListenerSet) return;
      recListenerSet = true;
      try {
        zoomSdk.onCloudRecording((evt) => {
          const a = (evt && evt.action) || "";   // 'connecting'|'started'|'paused'|'stopped'
          if (a !== "started" && a !== "paused" && a !== "stopped") return;
          recordEvent("phase", { phase: a, source: "zoom", zoom_ts: (evt && evt.timestamp) || null });
          applyRecStatus(a);
        });
      } catch (_) {}
    }

    // ── Decision capture + canonical chat broadcast ───────────────────────
    document.querySelectorAll(".outcome .oc").forEach((b) => {
      b.onclick = () => {
        selectedOutcome = b.getAttribute("data-oc");
        document.querySelectorAll(".outcome .oc").forEach((x) => x.classList.toggle("sel", x === b));
      };
    });

    function decisionChatText(d) {
      let s = "";
      if (d.considerations && d.considerations.length) {
        s += "Το Διοικητικό Συμβούλιο, έχοντας υπόψη:\\n";
        d.considerations.forEach((c, i) => { s += (i + 1) + ". " + c + "\\n"; });
        s += "\\n";
      }
      s += "ΑΠΟΦΑΣΗ " + d.ref + ": " + d.decision_text;
      if (d.outcome) s += "\\n[" + d.outcome + "]";
      return s;
    }

    function renderDecisions(list) {
      const ol = $("declist");
      ol.innerHTML = "";
      $("declist-label").textContent = "Καταγραφές (" + list.length + ")";
      if (!list.length) { ol.innerHTML = '<li style="border:none;color:#999;">Καμία απόφαση ακόμη.</li>'; return; }
      list.forEach((d) => {
        const yes = d.outcome === "Έγκριση";
        const li = document.createElement("li");
        li.innerHTML =
          '<span class="ref">' + (d.ref || "") + '</span>' +
          '<span>' + (d.decision_text || "").slice(0, 46) + '</span>' +
          '<span class="oc-tag ' + (yes ? "yes" : "no") + '">' + (d.outcome || "") + '</span>';
        ol.appendChild(li);
      });
    }
    async function loadDecisions() {
      try {
        const r = await fetch("/zoom-app/decisions?ref=" + encodeURIComponent(MEETING_REF));
        const data = await r.json();
        renderDecisions(data.decisions || []);
      } catch (_) { renderDecisions([]); }
    }

    $("dec-save").onclick = async () => {
      const text = $("dec-text").value.trim();
      if (!text) { toast("Συμπληρώστε το κείμενο της απόφασης."); return; }
      if (!selectedOutcome) { toast("Επιλέξτε Έγκριση ή Απόρριψη."); return; }
      const considerations = $("dec-cons").value.split("\\n").map((s) => s.trim()).filter(Boolean);
      $("dec-save").disabled = true;
      try {
        const r = await fetch("/zoom-app/decision", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            meeting_ref: MEETING_REF,
            decision_text: text,
            outcome: selectedOutcome,
            considerations: considerations,
            agenda_index: (!offAgenda && idx >= 0) ? idx : null,
            agenda_item: (!offAgenda && idx >= 0) ? AGENDA[idx] : "",
            pre_meeting: recState === "before",
          }),
        });
        const data = await r.json();
        if (!data.ok) throw new Error(data.error || "save failed");
        // Broadcast nicely to the meeting chat.
        try { await zoomSdk.sendMessageToChat({ message: decisionChatText(data.decision) }); } catch (_) {}
        $("dec-cons").value = ""; $("dec-text").value = "";
        selectedOutcome = "";
        document.querySelectorAll(".outcome .oc").forEach((x) => x.classList.remove("sel"));
        toast("Καταγράφηκε & ανακοινώθηκε - " + data.ref);
        await loadDecisions();
      } catch (e) {
        toast("Αποτυχία: " + e.message);
      } finally {
        $("dec-save").disabled = false;
      }
    };

    // ── Boot ──────────────────────────────────────────────────────────────
    async function loadAgenda(meetingTopic) {
      const m = (meetingTopic || "").match(/ΔΣ\\d{1,2}-\\d{4}/);
      const ref = m ? m[0] : "";
      try {
        const r = await fetch("/zoom-app/agenda?ref=" + encodeURIComponent(ref));
        const data = await r.json();
        AGENDA = data.items || [];
        MEETING_REF = data.meeting_ref || ref;
        if (data.meeting_ref) $("agenda-label").textContent = "Ημερήσια Διάταξη - " + data.meeting_ref;
      } catch (_) { AGENDA = []; }
      idx = -1; offAgenda = false; recState = "before";
      renderAgenda(); updateControls();
      $("panel").style.display = "block";
      await loadDecisions();
    }

    async function init() {
      try {
        const cfg = await zoomSdk.config({
          capabilities: [
            "getMeetingContext", "getUserContext",
            "setDynamicIndicator", "setDynamicIndicatorStyle", "removeDynamicIndicator",
            "sendMessageToChat", "showNotification",
            "cloudRecording", "getRecordingContext", "onCloudRecording",
          ],
          version: "0.16",
        });
        const topic = cfg.meetingTopic || "Συνεδρίαση ΔΣ";
        baseStatus = "Συνδέθηκε - " + topic;
        setStatus(baseStatus, true);
        ensureRecListener();
        try { const u = await zoomSdk.getUserContext(); screenName = u.screenName || ""; } catch (_) {}
        // Reflect the actual (auto-started) recording state.
        try {
          const rc = await zoomSdk.getRecordingContext();
          applyRecStatus((rc && (rc.cloudRecordingStatus || rc.status)) || "started");
        } catch (_) { applyRecStatus("started"); }
        await loadAgenda(topic);
      } catch (e) {
        setStatus("Σφάλμα σύνδεσης: " + e.message, false);
      }
    }

    zoomSdk.onRunningContextChange((ctx) => { if (ctx.runningContext === "inMeeting") init(); });
    init();
  </script>
</body>
</html>"""
