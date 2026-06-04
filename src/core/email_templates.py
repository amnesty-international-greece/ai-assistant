"""Load and render email body templates from ``assets/email_templates/``.

Templates are HTML files with ``{placeholder}`` slots filled via
``str.format``.  Keeps message copy out of Python source so non-engineers
can edit it without touching code — opens fine in any text editor.

Two rendering modes
-------------------
1. **Legacy (back-compat)**: pass only content kwargs.  The template's raw
   HTML is returned verbatim with placeholders substituted — used by the
   short single-paragraph templates (``minutes_share``, ``scheduling_*``).

2. **Shelled (v2 from 2026-05-27)**: pass ``kicker=`` and ``title=`` (and
   optionally ``header_ref=``, ``footer_note=``, ``stamp=``).  The inner
   template is wrapped in ``_shell.html`` — header (black + logo), yellow
   titlebar (kicker + headline), body slot, footer (candle + legal).
   Adopted by ``invitation_board`` and ``archive_confirmation`` so they
   pick up the Amnesty visual identity in a single edit.

Usage
-----
    from src.core.email_templates import render_email

    # Legacy plain-content render
    body = render_email(
        "scheduling_with_poll",
        meeting_ref="ΔΣ04-2026",
        poll_url="https://when2meet.com/...",
        sheet_url="https://docs.google.com/...",
        deadline="28/05",
    )

    # Shelled render (adds header / yellow titlebar / footer)
    body = render_email(
        "invitation_board",
        kicker="Πρόσκληση Διοικητικού Συμβουλίου",
        title="Επόμενη συνεδρίαση<br/>{greek_date}",
        greek_date="27 Μαΐου 2026",
        meeting_ref="ΔΣ04-2026",
        meeting_time="19:00",
        zoom_url="...",
        zoom_meeting_id="823 1234 5678",
        zoom_passcode="amnesty",
        share_link="...",
    )

Template files use single ``{name}`` braces for placeholders.  If a literal
``{`` or ``}`` is needed (e.g. inline CSS), double it: ``{{`` / ``}}``.  A
missing placeholder raises KeyError loudly (so a template typo fails fast
during workflow runs rather than producing silently-broken emails).
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any


def greek_upper(s: str) -> str:
    """Uppercase Greek text per typographic convention — tonos stripped.

    Greek convention: vowels in ALL CAPS drop the τόνος (e.g.
    "Προγραμματισμός" → "ΠΡΟΓΡΑΜΜΑΤΙΣΜΟΣ", not "ΠΡΟΓΡΑΜΜΑΤΙΣΜΌΣ").
    Python's built-in ``str.upper()`` follows Unicode-default case mapping
    which preserves the τόνος; CSS ``text-transform: uppercase`` strips it
    only when the rendering engine honours ``lang="el"`` (Chrome/Firefox do,
    many email clients don't).  This helper does it deterministically.

    Mechanics:
      * Decompose to NFD so combining accents are separate code points.
      * Drop U+0301 (combining acute) — this is Greek τόνος.
      * Preserve U+0308 (combining diaeresis / διαλυτικά) since dialytika
        in ALL CAPS *are* kept (e.g. "ΑΪ", "ΟΪ").
      * U+0344 (combining diaeresis-and-acute) → U+0308 (just diaeresis).
      * Uppercase the result, recompose to NFC.
    """
    decomposed = unicodedata.normalize("NFD", s)
    out_chars: list[str] = []
    for ch in decomposed:
        if ch == "́":      # tonos (combining acute) — drop
            continue
        if ch == "̈́":      # diaeresis + tonos → just diaeresis
            out_chars.append("̈")
            continue
        out_chars.append(ch)
    return unicodedata.normalize("NFC", "".join(out_chars)).upper()

_TEMPLATE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "assets" / "email_templates"
)
_SHELL_NAME = "_shell.html"

# Brand asset URLs — left unset by default.  Once the user uploads the logo
# and candle PNGs to Brevo's image library (or any public HTTPS host), they
# can pass them per-call via ``render_email(logo_url=..., candle_url=...)``.
#
# Until then, the shell renders a typographic fallback ("AMNESTY
# INTERNATIONAL" in Roboto Condensed Black, yellow on the black header
# strip) so emails look intentional instead of showing broken-image icons.
# The earlier defaults pointed at guessed amnesty.gr paths that turned out
# to be 404/403 — see 2026-05-27 user report.


def render_email(
    name: str,
    *,
    kicker: str | None = None,
    title: str | None = None,
    header_ref: str = "ΔΣ · AI ASSISTANT",
    footer_note: str = (
        "Σας στείλαμε αυτό το email αυτόματα από το AI Assistant "
        "του Ελληνικού Τμήματος της Διεθνούς Αμνηστίας."
    ),
    stamp: str | None = None,
    logo_url: str | None = None,
    candle_url: str | None = None,
    **kwargs: Any,
) -> str:
    """Render an email body template, optionally wrapped in the shared shell.

    Args:
        name:       Template stem (no path, no extension).
        kicker:     Small uppercase line above the headline.  Passing this
                    (or ``title``) opts into the shelled-render path.
        title:      Big headline in the yellow titlebar.  May contain
                    ``<br/>`` for line breaks; otherwise plain text only.
        header_ref: Small yellow text top-right of the black header.
        footer_note: Single line of legal/context text in the footer.
        stamp:      Optional stamp text rendered inside the yellow titlebar
                    (used by archive_confirmation for ΑΡ.ΠΡΩΤ.).
        logo_url:   Override the default Amnesty logo URL.
        candle_url: Override the default candle mark URL.
        **kwargs:   Values for ``{placeholder}`` slots in the inner template.

    Returns:
        Rendered HTML string ready to pass to ``send_email(body=..., html=True)``.

    Raises:
        FileNotFoundError: if the template file doesn't exist.
        KeyError:          if the template references a placeholder not in kwargs.
    """
    inner_path = _TEMPLATE_DIR / f"{name}.html"
    inner_template = inner_path.read_text(encoding="utf-8")
    inner_rendered = inner_template.format(**kwargs)

    # Legacy mode: no kicker/title → return inner content as-is.  Keeps the
    # short templates (minutes_share, scheduling_*) working unchanged.
    if kicker is None and title is None:
        return inner_rendered

    # Shelled mode — wrap inner content in the shared shell.
    stamp_html = (
        f'\n      <div class="stamp">{stamp}</div>' if stamp else ""
    )
    # The shell only needs a plain-text <title> for client tabs; strip any
    # HTML breaks from the visual title to keep it readable.
    title_plain = (title or "").replace("<br/>", " ").replace("<br>", " ")

    # Logo + candle: either an <img> if the caller passed a real URL, or a
    # typographic fallback so emails render cleanly without external images.
    # Both fallbacks are styled in the shell's CSS (``.logo-text``).
    logo_html = (
        f'<img src="{logo_url}" alt="ΔΙΕΘΝΗΣ ΑΜΝΗΣΤΙΑ - ΕΛΛΗΝΙΚΟ ΤΜΗΜΑ" />'
        if logo_url
        else '<span class="logo-text">ΔΙΕΘΝΗΣ ΑΜΝΗΣΤΙΑ - ΕΛΛΗΝΙΚΟ ΤΜΗΜΑ</span>'
    )
    candle_html = (
        f'<img class="candle-mark" src="{candle_url}" alt="" />'
        if candle_url
        else ""  # Footer gracefully degrades to text-only without the candle.
    )

    # Kicker and header_ref render in ALL CAPS via CSS — but CSS text-transform
    # is inconsistent across email clients for Greek (tonos handling).  Pre-
    # uppercase them in Python so the HTML carries the correct typography.
    kicker_uc = greek_upper(kicker) if kicker else ""
    header_ref_uc = greek_upper(header_ref)

    shell = (_TEMPLATE_DIR / _SHELL_NAME).read_text(encoding="utf-8")
    return shell.format(
        title=title or "",
        title_plain=title_plain,
        kicker=kicker_uc,
        header_ref=header_ref_uc,
        footer_note=footer_note,
        stamp_html=stamp_html,
        logo_html=logo_html,
        candle_html=candle_html,
        body=inner_rendered,
    )
