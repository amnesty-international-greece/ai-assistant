"""No role mailbox may be hard-coded in the platform's code.

Role addresses (board, director, secgen, members) belong to the section, not
to the platform: they change when a section adopts it, and a typo in one is
invisible until an email silently goes nowhere (as `board@amnesty.gr`, missing
the `.org`, did). They live in config under `roles:`.

Only executable string literals are checked; comments and docstrings may still
name an address when explaining what a workflow does.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from src.config import settings

SRC = Path(__file__).resolve().parent.parent / "src"
EMAIL = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+")
# Addresses that are obviously not a real mailbox of a real organisation.
FAKE_DOMAINS = ("example.com", "example.org", "example.net", "test.local")


def _configured() -> set[str]:
    allowed = {v.lower() for v in vars(settings.roles).values() if isinstance(v, str)}
    for extra in (settings.brevo.sender_email, settings.testing.test_email,
                  settings.discord.email_gateway.gmail_user,
                  settings.discord.email_gateway.google_group_email):
        if extra:
            allowed.add(extra.lower())
    return allowed


def _docstrings(tree: ast.AST) -> set[int]:
    """id() of every docstring node, so they can be skipped."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def test_no_role_address_is_hard_coded_in_code():
    allowed = _configured()
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name == "config.py":
            continue  # the model's defaults are the configuration, not a call site
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in skip:
                continue
            for address in EMAIL.findall(node.value):
                low = address.lower()
                if low.endswith(FAKE_DOMAINS) or low in allowed:
                    continue
                offenders.append(f"{path.relative_to(SRC.parent)}:{node.lineno}: {address}")
    assert not offenders, (
        "Hard-coded role addresses (use settings.roles.* instead):\n  "
        + "\n  ".join(offenders)
    )


def test_configured_role_addresses_share_one_domain():
    """A typo like `board@amnesty.gr` vs `members@amnesty.org.gr` shows up here."""
    domains = {v.split("@")[-1].lower() for v in vars(settings.roles).values()
               if isinstance(v, str) and "@" in v}
    assert len(domains) == 1, f"Role mailboxes span several domains: {sorted(domains)}"
