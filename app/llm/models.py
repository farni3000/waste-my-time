"""Auflösung des DeepSeek-Modells.

DeepSeek pflegt für die Pro-Klasse keinen versionslosen Alias (anders als
früher `deepseek-chat`, das inzwischen auf Flash zeigt). Damit die App trotzdem
nicht auf einer datierten Snapshot-ID einfriert, wird das Modell über ein Tier
(`pro`/`flash`) aus `GET /models` aufgelöst: neuestes Modell der Klasse, das der
Key tatsächlich anbietet. Der Katalog wird in `api_cache` zwischengespeichert,
`DEEPSEEK_MODEL` bleibt als harter Pin-Override.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..config import get_settings
from ..sources.store import _get_cached, _put_cached

MODELS_CACHE_KEY = "deepseek:models"
MODELS_CACHE_DAYS = 7

TIER_TOKENS: dict[str, tuple[str, ...]] = {
    "pro": ("pro",),
    "flash": ("flash", "chat"),
}

# Stand der API zum Zeitpunkt der Implementierung. Greift nur, wenn /models
# nicht erreichbar ist oder das Namensschema einmal nicht mehr passt.
TIER_FALLBACK: dict[str, str] = {
    "pro": "deepseek-v4-pro",
    "flash": "deepseek-flash",
}

_VERSION_RE = re.compile(r"v(?:ersion)?\s*(\d+(?:\.\d+)*)", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[^a-z0-9]+")

_catalog_cache: list[dict[str, Any]] | None = None


def _version_key(name: str) -> tuple[int, ...]:
    match = _VERSION_RE.search(name or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _entry_label(entry: dict[str, Any]) -> str:
    return f"{entry.get('name') or ''} {entry.get('id') or ''}"


def _entry_tokens(entry: dict[str, Any]) -> set[str]:
    return {token for token in _TOKEN_RE.split(_entry_label(entry).lower()) if token}


def pick_model(catalog: list[dict[str, Any]], tier: str) -> str | None:
    """Wählt das neueste Modell der Klasse `tier` aus einem `/models`-Katalog."""
    tokens = TIER_TOKENS.get(tier)
    if not tokens:
        return None
    matches = [
        entry
        for entry in catalog
        if isinstance(entry, dict)
        and entry.get("id")
        and any(token in _entry_tokens(entry) for token in tokens)
    ]
    if not matches:
        return None
    best = max(matches, key=lambda entry: (_version_key(_entry_label(entry)), str(entry["id"])))
    return str(best["id"])


def supported_effort_levels(catalog: list[dict[str, Any]], model_id: str) -> tuple[str, ...]:
    for entry in catalog:
        if isinstance(entry, dict) and entry.get("id") == model_id:
            effort = entry.get("effort") or {}
            levels = effort.get("supported_levels") or ()
            return tuple(str(level) for level in levels)
    return ()


def _fetch_catalog() -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.deepseek_api_key:
        return []
    url = f"{settings.deepseek_base_url.rstrip('/')}/models"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            timeout=settings.request_timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    entries = data.get("data") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("id")]


def load_catalog(session: Session | None = None) -> list[dict[str, Any]]:
    """Liefert den Modellkatalog, bevorzugt aus dem Prozess-, dann dem DB-Cache."""
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache

    cached = _get_cached(session, MODELS_CACHE_KEY) if session is not None else None
    if isinstance(cached, list) and cached:
        _catalog_cache = cached
        return _catalog_cache

    catalog = _fetch_catalog()
    if catalog:
        if session is not None:
            _put_cached(session, MODELS_CACHE_KEY, catalog, days=MODELS_CACHE_DAYS)
        _catalog_cache = catalog
    return catalog


def resolve_model(session: Session | None = None) -> str:
    """Konkretes Modell: Pin aus der Config, sonst neuestes Modell des Tiers."""
    settings = get_settings()
    pinned = settings.deepseek_model.strip()
    if pinned:
        return pinned

    tier = settings.deepseek_model_tier
    catalog = load_catalog(session)
    return pick_model(catalog, tier) or TIER_FALLBACK[tier]


def effective_effort(session: Session | None = None, model_id: str | None = None) -> str:
    """Reasoning-Effort nur senden, wenn das Modell ihn laut Katalog unterstützt."""
    effort = get_settings().llm_reasoning_effort
    if not effort:
        return ""
    model = model_id or resolve_model(session)
    if effort not in supported_effort_levels(load_catalog(session), model):
        return ""
    return effort


def reset_cache() -> None:
    """Nur für Tests: Prozess-Cache leeren."""
    global _catalog_cache
    _catalog_cache = None
