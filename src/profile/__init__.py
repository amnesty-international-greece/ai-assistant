"""The section this deployment serves: its identity, wording and rules."""

from src.profile.loader import (
    DEFAULT_SECTION,
    Section,
    SectionProfile,
    SectionRules,
    load_section,
)

__all__ = [
    "DEFAULT_SECTION",
    "Section",
    "SectionProfile",
    "SectionRules",
    "load_section",
    "section",
]


def __getattr__(name: str):
    """``from src.profile import section`` loads it once, on first use."""
    if name == "section":
        global section
        from src.config import settings

        section = load_section(settings.section)
        return section
    raise AttributeError(name)
