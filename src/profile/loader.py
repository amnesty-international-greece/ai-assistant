"""The section profile: who this deployment belongs to, and by what rules.

The platform should know that a board has a quorum, that meetings carry a
reference, that documents are filed under a number. It should not know that
the board is called Διοικητικό Συμβούλιο, that its references start with ΔΣ,
or that five members make a quorum. Those belong to the Greek Section, and to
any other section they would be different.

They live in ``sections/<slug>/``:

* ``profile.yaml`` - identity, roles, names and the wording of the section;
* ``rules.yaml``   - what the statute requires: quorum, notice, cadence;
* ``roster.yaml``  - the people (gitignored: it is personal data).

``sections/test-en/`` is a second, fictional section in English. It exists so
the test suite can prove the platform reads these values rather than carrying
the Greek ones in its code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

logger = logging.getLogger(__name__)

SECTIONS_DIR = Path(__file__).resolve().parent.parent.parent / "sections"
DEFAULT_SECTION = "amnesty-gr"


class Identity(BaseModel):
    """How the section names itself on documents and in messages."""
    organisation: str = "Amnesty International"
    section: str = ""
    display_name: str = "Amnesty International"
    legal_name: str = ""
    website: str = ""
    email: str = ""
    phone: str = ""
    address_lines: list[str] = []
    office_location: str = ""        # mid-sentence, e.g. "at 30 Sina St, 2nd floor"
    aliases: list[str] = []          # how the organisation is said aloud, for ASR


class BoardProfile(BaseModel):
    """The governing body: what it is called and how its references read."""
    name: str = "Board of Directors"
    # Greek (and other inflected languages) need the genitive in prose:
    # "αποφάσεων του Διοικητικού Συμβουλίου". Defaults to `name`.
    name_genitive: str = ""
    short_name: str = "Board"
    ref_prefix: str = "BD"
    seats: int = 9


class SectionProfile(BaseModel):
    slug: str = DEFAULT_SECTION
    locale: str = "en"
    identity: Identity = Identity()
    board: BoardProfile = BoardProfile()
    roles: dict[str, str] = {}       # role -> mailbox
    assets: dict[str, str] = {}      # prompts, templates, governance corpus


class BoardRules(BaseModel):
    """What the statute requires of the board. Article references included."""
    quorum: int = 0                  # 0 = not stated; nothing is enforced
    quorum_article: str = ""
    min_notice_days: int = 0
    max_advance_days: int = 0
    min_meetings_per_year: int = 0
    absences_forfeiting_seat: int = 0
    consecutive_absences_forfeiting_seat: int = 0


class AssemblyRules(BaseModel):
    min_notice_days: int = 0
    min_electronic_notice_days: int = 0
    quorum_required: bool = False


class SectionRules(BaseModel):
    board: BoardRules = BoardRules()
    assembly: AssemblyRules = AssemblyRules()


class Section(BaseModel):
    """A section profile and its rules, loaded together."""
    profile: SectionProfile = SectionProfile()
    rules: SectionRules = SectionRules()

    def asset_path(self, key: str, default: str = "") -> Path:
        """Where the section keeps one kind of asset.

        Prompts, email templates and the governance corpus are the section's
        own words: its prompts speak its language, its templates carry its
        letterhead, its corpus is its statute. ``profile.assets`` names them;
        a relative path is taken from the repository root.
        """
        raw = str(self.profile.assets.get(key) or default or "").strip()
        if not raw:
            raise KeyError(f"Section {self.slug!r} defines no asset {key!r}")
        path = Path(raw)
        return path if path.is_absolute() else SECTIONS_DIR.parent / path

    @property
    def slug(self) -> str:
        return self.profile.slug

    @property
    def name(self) -> str:
        return self.profile.identity.display_name


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}


def load_section(slug: str = DEFAULT_SECTION, *, root: Path | None = None) -> Section:
    """Load ``sections/<slug>/profile.yaml`` and ``rules.yaml``.

    A missing folder is not fatal: the defaults describe a section that has
    told the platform nothing, so nothing section-specific is assumed.
    """
    folder = (root or SECTIONS_DIR) / slug
    if not folder.exists():
        logger.warning("No section profile at %s - falling back to defaults", folder)
        return Section()
    profile_data = _read(folder / "profile.yaml")
    profile_data.setdefault("slug", slug)
    return Section(
        profile=SectionProfile(**profile_data),
        rules=SectionRules(**_read(folder / "rules.yaml")),
    )
