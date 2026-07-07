"""Render EVERYTHING visual into one screenshot-ready gallery for the board deck.

    python scripts/render_gallery.py
    -> opens data/preview/index.html

Includes:
  * HTML emails (invitation, scheduling, minutes, archive) - real renders
  * Discord embeds (thread, scheduling/invitation mirror, public agenda,
    reminder, minutes, cancellation, εγκύκλιος) - rendered as Discord-styled cards
  * The Zoom in-meeting sidebar - a populated demo state

Open index.html in a browser, then screenshot each block for the presentation.
"""
from __future__ import annotations

import datetime
import html as _html
import subprocess
import sys
import webbrowser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "preview"
ATHENS = ZoneInfo("Europe/Athens")
_STARTS = datetime.datetime(2026, 6, 9, 20, 0, tzinfo=ATHENS)
_AGENDA = "1. Επικύρωση πρακτικών\n2. Ενημέρωση Γραφείου\n3. Πρόγραμμα αυτοματοποίησης"

EMAIL_LABELS = {
    "invitation_board": "Email - Πρόσκληση στο ΔΣ",
    "scheduling_with_poll": "Email - Προγραμματισμός (με poll)",
    "scheduling_no_poll": "Email - Προγραμματισμός (χωρίς poll)",
    "minutes_share": "Email - Κοινοποίηση πρακτικών",
    "archive_confirmation": "Email - Επιβεβαίωση αρχειοθέτησης",
    "archive_failure": "Email - Αποτυχία αρχειοθέτησης",
}


# Brand PNGs copied into data/preview/assets/ so index.html is self-contained.
_BRAND = ROOT / "brand" / "Logo"
BRAND_ASSETS = {
    "logo_white.png": _BRAND / "Amnesty Logo" / "ENG_Amnesty_logo_RGB"
    / "ENG_Amnesty_logo_RGB_white" / "ENG_Amnesty_logo_RGB_white.png",
    "logo_black.png": _BRAND / "Amnesty Logo" / "ENG_Amnesty_logo_RGB"
    / "ENG_Amnesty_logo_RGB_black" / "ENG_Amnesty_logo_RGB_black.png",
    "candle_yellow.png": _BRAND / "Amnesty Candle"
    / "Amnesty_candle_RGB_Yellow" / "Amnesty_candle_RGB_Yellow.png",
    "candle_black.png": _BRAND / "Amnesty Candle"
    / "Amnesty_candle_transparent_background" / "Amnesty_candle_RGB_Black.png",
}

# Dash family + middle dot + Greek ano teleia -> ASCII hyphen, per house style
# (no em/en dashes, no "middot" anywhere). Defined by code point so this source
# file itself stays free of the very characters it strips.
_FLATTEN = {cp: "-" for cp in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014,
                               0x2015, 0x2212, 0x00B7, 0x0387)}


def _dedash(text: str) -> str:
    return text.translate(_FLATTEN)


def _esc(s) -> str:
    return _html.escape(str(s)).replace("\n", "<br/>")


def _embed_card(title: str, pair) -> str:
    """Render a (embed, view) or embed into a Discord-styled HTML card."""
    embed = pair[0] if isinstance(pair, tuple) else pair
    view = pair[1] if isinstance(pair, tuple) and len(pair) > 1 else None
    colour = f"#{embed.colour.value:06x}" if getattr(embed, "colour", None) else "#5865F2"

    parts = [f'<div class="dcard" style="border-left-color:{colour}">']
    if getattr(embed, "author", None) and embed.author.name:
        parts.append(f'<div class="dauthor">{_esc(embed.author.name)}</div>')
    if embed.title:
        parts.append(f'<div class="dtitle">{_esc(embed.title)}</div>')
    if embed.description:
        parts.append(f'<div class="ddesc">{_esc(embed.description)}</div>')
    if embed.fields:
        parts.append('<div class="dfields">')
        for fld in embed.fields:
            inl = "inline" if fld.inline else "block"
            name = f'<div class="dfname">{_esc(fld.name)}</div>' if fld.name and fld.name.strip() else ""
            parts.append(f'<div class="dfield {inl}">{name}<div class="dfval">{_esc(fld.value)}</div></div>')
        parts.append('</div>')
    if getattr(embed, "footer", None) and embed.footer.text:
        parts.append(f'<div class="dfooter">{_esc(embed.footer.text)}</div>')
    if view is not None:
        btns = []
        for item in getattr(view, "children", []):
            label = getattr(item, "label", "") or ""
            btns.append(f'<span class="dbtn">{_esc(label)} ↗</span>')
        if btns:
            parts.append('<div class="dbtns">' + "".join(btns) + '</div>')
    parts.append('</div>')
    return f'<div class="block"><h3>{_esc(title)}</h3>{"".join(parts)}</div>'


def _discord_section() -> str:
    import src.integrations.discord.embeds.board_meeting as bm
    import src.integrations.discord.embeds.egkyklios as eg

    ref = "ΔΣ05-2026"
    sheet = "https://docs.google.com/spreadsheets/d/example"
    zoom = "https://us06web.zoom.us/j/82264596638"
    cards = [
        ("Mirror - Προγραμματισμός", bm.scheduling_mirror_embed(
            meeting_ref=ref, poll_url="https://crab.fit/synedriasi-ds05", agenda_url=sheet)),
        ("Δημόσιο thread - Πρόσκληση", bm.public_invitation_embed(
            starts_at=_STARTS, agenda_summary=_AGENDA, zoom_url=zoom)),
        ("Mirror - Πρόσκληση ΔΣ", bm.invitation_mirror_embed(
            meeting_ref=ref, zoom_url=zoom, agenda_url=sheet,
            invitation_pdf_url="https://amnestygr.sharepoint.com/share/xyz",
            meeting_datetime="2026-06-09T20:00", agenda_summary=_AGENDA)),
        ("Υπενθύμιση", bm.reminder_embed(hours_before=6, starts_at=_STARTS)),
        ("Mirror - Πρόχειρα πρακτικά", bm.minutes_mirror_embed(
            meeting_ref=ref, doc_url="https://docs.google.com/document/d/ex", is_draft=True)),
        ("Δημοσίευση πρακτικών", bm.minutes_shared_embed(drive_url="https://drive.google.com/ex")),
        ("Ακύρωση", bm.cancellation_embed(reason="Αναβλήθηκε λόγω απαρτίας.")),
        ("Γενική Εγκύκλιος", eg.egkyklios_published_embed(
            kind="Ενημερωτική", title="Τριμηνιαία Ενημέρωση Q2 2026",
            protocol_number="2026_030", sent_at="9 Ιουνίου 2026",
            sharepoint_url="https://amnestygr.sharepoint.com/ex")),
    ]
    inner = "".join(_embed_card(t, p) for t, p in cards)
    return f'<section class="discord"><h2>Discord</h2><div class="grid">{inner}</div></section>'


def _emails_section() -> str:
    blocks = []
    for stem, label in EMAIL_LABELS.items():
        f = OUT / f"{stem}.html"
        if f.exists():
            blocks.append(
                f'<div class="block"><h3>{_esc(label)}</h3>'
                f'<iframe class="email" src="{stem}.html" loading="lazy"></iframe></div>'
            )
    return f'<section class="emails"><h2>Emails</h2><div class="grid">{"".join(blocks)}</div></section>'


def _zoom_section() -> str:
    items = ["Επικύρωση πρακτικών", "Ενημέρωση Γραφείου", "Πρόγραμμα αυτοματοποίησης",
             "Πλαίσιο Εθνικού Στρατηγικού Πλάνου", "Διαδικασίες υποψηφιοτήτων"]
    lis = "".join(
        f'<li class="{"cur" if i == 1 else ""}"><span class="n">{i+1}</span>{_esc(it)}</li>'
        for i, it in enumerate(items))
    return f'''<section class="zoom"><h2>Zoom - Πλαϊνό panel</h2>
      <div class="zside">
        <div class="zhead">AI Assistant - ΔΣ</div>
        <div class="zrec">● ΕΓΓΡΑΦΗ ΕΝΕΡΓΗ</div>
        <p class="zlbl">Ημερήσια Διάταξη - ΔΣ05-2026</p>
        <ol class="zagenda">{lis}</ol>
        <div class="zctrls"><button class="zalt">‹ ΠΡΟΗΓΟΥΜΕΝΟ</button><button>ΕΠΟΜΕΝΟ ›</button></div>
        <hr/>
        <p class="zlbl">Καταγραφή Απόφασης - Θέμα 2</p>
        <div class="ztext">Εγκρίνει την έκτακτη πλήρωση της θέσης του Υπεύθυνου HR.</div>
        <div class="zout"><button class="zy">ΕΓΚΡΙΣΗ</button><button>ΑΠΟΡΡΙΨΗ</button></div>
        <p class="zlbl" style="margin-top:14px;">Καταγραφές (1)</p>
        <div class="zdec"><b>ΔΣ01-05-2026</b> - Έγκριση… <span class="zyes">Έγκριση</span></div>
      </div></section>'''


def _header() -> str:
    """Black brand banner: yellow candle + 'AI Assistant' + white Amnesty logo."""
    return (
        '<header class="hero">'
        '<img class="candle" src="assets/candle_yellow.png" alt="Amnesty candle"/>'
        '<div class="hero-mid">'
        '<h1>AI Assistant</h1>'
        '<p class="hero-sub">Πλατφόρμα αυτοματοποίησης διακυβέρνησης ΔΣ</p>'
        '</div>'
        '<img class="hero-logo" src="assets/logo_white.png" alt="Amnesty International"/>'
        '</header>'
        '<p class="sub">Δείγματα όλων των επικοινωνιών για την παρουσίαση στο ΔΣ. '
        'Κάντε screenshot το κάθε block.</p>'
    )


def _footer() -> str:
    return (
        '<footer class="foot">'
        '<img class="foot-candle" src="assets/candle_black.png" alt=""/>'
        '<span>Διεθνής Αμνηστία - Ελληνικό Τμήμα - AI Assistant</span>'
        '</footer>'
    )


_CSS = """
  body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f5f3ee;color:#0a0a0a;margin:0;padding:24px;}
  .hero{background:#0a0a0a;display:flex;align-items:center;gap:22px;padding:22px 28px;border-radius:6px;}
  .hero .candle{height:64px;width:auto;display:block;}
  .hero-mid{flex:1;}
  .hero h1{font-size:30px;margin:0;color:#fff;letter-spacing:.01em;}
  .hero-sub{margin:4px 0 0;color:#FFFF00;font-size:13px;font-weight:600;}
  .hero-logo{height:40px;width:auto;display:block;opacity:.95;}
  .foot{display:flex;align-items:center;gap:10px;margin:40px 0 8px;color:#777;font-size:12px;border-top:1px solid #ddd;padding-top:14px;}
  .foot-candle{height:26px;width:auto;}
  h1{font-size:22px;margin:0 0 4px;} .sub{color:#777;margin:14px 0 24px;font-size:13px;}
  h2{margin:34px 0 14px;border-bottom:3px solid #FFFF00;display:inline-block;padding-bottom:2px;}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:18px;}
  .block h3{font-size:13px;color:#555;margin:0 0 8px;font-weight:600;}
  iframe.email{width:100%;height:560px;border:1px solid #ddd;background:#fff;}
  /* Discord */
  .discord .grid{grid-template-columns:repeat(auto-fill,minmax(420px,1fr));}
  .dcard{background:#2b2d31;color:#dbdee1;border-left:4px solid #5865F2;border-radius:4px;padding:12px 16px;font-size:14px;line-height:1.4;}
  .dauthor{font-size:12px;color:#b5bac1;margin-bottom:4px;}
  .dtitle{font-weight:700;color:#fff;margin-bottom:6px;}
  .ddesc{color:#dbdee1;margin-bottom:8px;}
  .dfields{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0;}
  .dfield.block{flex:1 1 100%;} .dfield.inline{flex:1 1 40%;}
  .dfname{font-size:12px;font-weight:700;color:#fff;margin-bottom:2px;}
  .dfval{font-size:13px;color:#dbdee1;}
  .dfooter{font-size:11px;color:#949ba4;margin-top:10px;border-top:1px solid #3a3c41;padding-top:6px;}
  .dbtns{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;}
  .dbtn{background:#4e5058;color:#fff;border-radius:4px;padding:7px 12px;font-size:13px;font-weight:600;}
  /* Zoom */
  .zside{width:300px;background:#f5f3ee;border:1px solid #ccc;padding:14px;}
  .zhead{background:#0a0a0a;color:#FFFF00;padding:10px 14px;margin:-14px -14px 12px;font-weight:700;font-size:12px;letter-spacing:.1em;}
  .zrec{background:#fdecea;color:#E63B11;font-weight:700;font-size:12px;padding:7px 10px;margin-bottom:12px;}
  .zlbl{font-size:11px;letter-spacing:.1em;text-transform:uppercase;font-weight:700;color:#888;margin:0 0 8px;}
  ol.zagenda{list-style:none;margin:0 0 12px;padding:0;}
  ol.zagenda li{display:flex;gap:8px;align-items:center;padding:8px 10px;border:1px solid #e3e0d8;border-bottom:none;background:#fff;font-size:13px;}
  ol.zagenda li:last-child{border-bottom:1px solid #e3e0d8;}
  ol.zagenda li.cur{background:#FFFF00;border-color:#0a0a0a;font-weight:600;}
  ol.zagenda li .n{flex:0 0 20px;height:20px;border-radius:50%;background:#0a0a0a;color:#FFFF00;font-size:11px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;}
  .zctrls,.zout{display:flex;gap:8px;margin-bottom:8px;}
  .zside button{flex:1;padding:10px;border:none;font-weight:700;font-size:11px;background:#0a0a0a;color:#FFFF00;cursor:default;}
  .zside button.zalt{background:#FFFF00;color:#0a0a0a;border:1px solid #0a0a0a;}
  .zside button.zy{background:#fff;color:#0a0a0a;border:1px solid #0a0a0a;}
  .ztext{border:1px solid #cfcabd;background:#fff;padding:8px 10px;font-size:13px;margin-bottom:8px;}
  .zdec{font-size:12px;border-bottom:1px solid #eee;padding:6px 0;}
  .zyes{background:#FFFF00;padding:1px 5px;font-weight:700;float:right;}
  hr{border:none;border-top:1px solid #e3e0d8;margin:14px 0;}
"""


def _copy_brand_assets() -> None:
    """Copy the brand PNGs the gallery references into data/preview/assets/."""
    import shutil
    dest = OUT / "assets"
    dest.mkdir(parents=True, exist_ok=True)
    for name, src in BRAND_ASSETS.items():
        if src.exists():
            shutil.copyfile(src, dest / name)
        else:
            print(f"  ! missing brand asset: {src}")


def _dedash_email_files() -> None:
    """Rewrite each rendered email HTML with em/en-dashes flattened to '-'."""
    for stem in EMAIL_LABELS:
        f = OUT / f"{stem}.html"
        if f.exists():
            f.write_text(_dedash(f.read_text(encoding="utf-8")), encoding="utf-8")


def main() -> None:
    print("Rendering emails...")
    subprocess.run([sys.executable, "scripts/preview_email.py"], cwd=ROOT, check=False)
    OUT.mkdir(parents=True, exist_ok=True)
    _copy_brand_assets()
    _dedash_email_files()

    body = _emails_section() + _discord_section() + _zoom_section()
    page = (
        "<!DOCTYPE html><html lang='el'><head><meta charset='utf-8'>"
        "<title>AI Assistant - Gallery</title><style>" + _CSS + "</style></head><body>"
        + _header() + body + _footer() + "</body></html>"
    )
    page = _dedash(page)
    out = OUT / "index.html"
    out.write_text(page, encoding="utf-8")
    print(f"Wrote {out}")
    webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
