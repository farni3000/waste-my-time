from __future__ import annotations

import re
import unicodedata

_EDITION_SUFFIXES = [
    "game of the year edition",
    "game of the year",
    "goty edition",
    "goty",
    "definitive edition",
    "ultimate edition",
    "deluxe edition",
    "complete edition",
    "enhanced edition",
    "special edition",
    "digital deluxe edition",
    "standard edition",
    "remastered",
    "hd remaster",
]


def normalize_title(title: str) -> str:
    text = (title or "").replace("™", "").replace("®", "").replace("©", "")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    changed = True
    while changed and text:
        changed = False
        for suffix in _EDITION_SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                changed = True
            elif text == suffix:
                text = ""
                changed = True
    return text


def parse_tag_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[,;\n]", raw)
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        tag = part.strip()
        key = tag.lower()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def parse_hours(raw: str | float | None) -> int:
    """Parse a human hours value into minutes (accepts '12,5' and '12.5')."""
    if raw is None or raw == "":
        return 0
    if isinstance(raw, int | float):
        return int(round(float(raw) * 60))
    value = raw.strip().lower().replace("h", "").replace(",", ".")
    try:
        return int(round(float(value) * 60))
    except ValueError:
        return 0


def format_hours(minutes: int | None) -> str:
    if not minutes:
        return "0 h"
    hours = minutes / 60
    if hours < 10:
        return f"{hours:.1f} h".replace(".0 h", " h")
    return f"{hours:.0f} h"


def title_tokens(title: str) -> set[str]:
    stop = {"the", "a", "an", "of", "and", "in", "to", "edition", "remastered"}
    return {token for token in normalize_title(title).split() if token not in stop and len(token) > 2}
