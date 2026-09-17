"""Generated two-word slugs for paper bots. Display as title case."""

from __future__ import annotations

import random
import re
from collections.abc import Iterable

ADJECTIVES = (
    "quiet", "copper", "swift", "amber", "silent", "steady", "bright", "calm",
    "north", "silver", "clear", "bold", "gentle", "rapid", "solemn", "vivid",
    "lunar", "solar", "hidden", "open", "brisk", "still", "iron", "ivory",
    "azure", "ember", "frost", "moss", "cedar", "marble", "nimbus", "tidal",
)

NOUNS = (
    "harbor", "current", "beacon", "ridge", "grove", "signal", "ledger", "anchor",
    "meadow", "cinder", "willow", "quartz", "basin", "delta", "summit", "channel",
    "mirror", "compass", "orbit", "vector", "isthmus", "canyon", "spire", "inlet",
    "prairie", "timber", "cascade", "horizon", "meridian", "strait", "reef", "dune",
)

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,31}")


def slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return text[:32]


def display_name(slug: str) -> str:
    parts = [part for part in (slug or "").split("-") if part]
    return " ".join(part.capitalize() for part in parts) or slug


def suggest_name(taken: Iterable[str] | None = None) -> str:
    used = {slugify(name) for name in (taken or ()) if name}
    for _ in range(400):
        slug = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
        if slug not in used and NAME_RE.fullmatch(slug):
            return slug
    for index in range(1, 1000):
        slug = f"paper-bot-{index}"
        if slug not in used:
            return slug
    return "paper-bot"


STOP_WORDS = {
    "my", "the", "our", "this", "that", "his", "her", "its", "from", "on", "to",
    "go", "of", "a", "an", "for", "and", "roster", "desk", "agent", "agents",
    "bot", "bots", "trader", "person", "guy", "one", "please", "hey", "okay",
    "ok", "actually", "want", "make", "named", "called",
}


def resolve_agent_name(text: str, names: Iterable[str]) -> str | None:
    available = [name for name in names if name]
    if not available:
        return None
    needle = slugify(text)
    if needle:
        for name in available:
            if name == needle or slugify(name) == needle:
                return name
    blob = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    compact = blob.replace(" ", "")
    for name in available:
        spaced = name.replace("-", " ")
        if spaced in blob or name in compact:
            return name
    tokens = [word for word in blob.split() if word not in STOP_WORDS and len(word) > 1]
    hits = []
    for name in available:
        parts = set(name.split("-"))
        if any(token == name or token in parts for token in tokens):
            hits.append(name)
    if len(hits) == 1:
        return hits[0]
    matches = [name for name in available if needle and (needle in name or name in needle)]
    if len(matches) == 1:
        return matches[0]
    return None
