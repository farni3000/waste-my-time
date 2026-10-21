from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ApiCache, Game
from ..timeutil import utcnow
from .http import USER_AGENT, polite_delay

STORE_URL = "https://store.steampowered.com/api/appdetails"
STEAMSPY_URL = "https://steamspy.com/api.php"
REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
SEARCH_URL = "https://store.steampowered.com/api/storesearch/"

CACHE_DAYS = 30


def _get_cached(session: Session, key: str, allow_stale: bool = False) -> Any | None:
    entry = session.get(ApiCache, key)
    if entry is None:
        return None
    if entry.expires_at and entry.expires_at < utcnow():
        return entry.body_json if allow_stale else None
    return entry.body_json


def _put_cached(session: Session, key: str, body: Any, days: int = CACHE_DAYS) -> None:
    entry = session.get(ApiCache, key)
    if entry is None:
        entry = ApiCache(key=key, body_json=body)
        session.add(entry)
    else:
        entry.body_json = body
    entry.fetched_at = utcnow()
    entry.expires_at = utcnow() + timedelta(days=days)
    session.commit()


def _get_json(url: str, params: dict[str, Any] | None = None, delay: float = 0.3) -> Any:
    polite_delay(url, delay)
    response = httpx.get(
        url,
        params=params,
        timeout=get_settings().request_timeout,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.json()


def fetch_appdetails(session: Session, appid: int, force: bool = False) -> dict | None:
    key = f"store:appdetails:{appid}"
    if not force:
        cached = _get_cached(session, key)
        if cached is not None:
            return cached
    try:
        payload = _get_json(STORE_URL, {"appids": appid, "cc": "de", "l": "german"})
        data = (payload or {}).get(str(appid), {}).get("data")
    except Exception:
        return _get_cached(session, key, allow_stale=True)
    if data:
        _put_cached(session, key, data)
    return data


def fetch_steamspy(session: Session, appid: int, force: bool = False) -> dict | None:
    key = f"steamspy:appdetails:{appid}"
    if not force:
        cached = _get_cached(session, key)
        if cached is not None:
            return cached
    try:
        data = _get_json(STEAMSPY_URL, {"request": "appdetails", "appid": appid}, delay=0.4)
    except Exception:
        return _get_cached(session, key, allow_stale=True)
    if data:
        _put_cached(session, key, data)
    return data


def fetch_review_summary(session: Session, appid: int, force: bool = False) -> dict | None:
    key = f"reviews:{appid}"
    if not force:
        cached = _get_cached(session, key)
        if cached is not None:
            return cached
    try:
        data = _get_json(
            REVIEWS_URL.format(appid=appid),
            {"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
            delay=0.3,
        )
    except Exception:
        return _get_cached(session, key, allow_stale=True)
    summary = (data or {}).get("query_summary")
    if summary:
        _put_cached(session, key, summary)
    return summary


def fetch_steamspy_tag(session: Session, tag: str, force: bool = False) -> dict[int, str]:
    key = f"steamspy:tag:{tag.lower()}"
    cached = None if force else _get_cached(session, key)
    if cached is None:
        try:
            payload = _get_json(
                STEAMSPY_URL, {"request": "tag", "tag": tag}, delay=1.0
            )
            cached = {}
            if isinstance(payload, dict):
                for appid, entry in payload.items():
                    try:
                        appid_int = int(appid)
                    except (TypeError, ValueError):
                        continue
                    name = entry.get("name") if isinstance(entry, dict) else None
                    if name:
                        cached[appid_int] = name
            elif isinstance(payload, list):
                for entry in payload:
                    if isinstance(entry, dict) and entry.get("appid") and entry.get("name"):
                        cached[int(entry["appid"])] = entry["name"]
            if not cached:
                cached = _get_cached(session, key, allow_stale=True) or {}
            _put_cached(session, key, cached, days=7)
        except Exception:
            cached = _get_cached(session, key, allow_stale=True) or {}
    return {int(key): value for key, value in cached.items()}


def search_store(session: Session, term: str) -> list[dict]:
    key = f"store:search:{term.lower().strip()}"
    cached = _get_cached(session, key)
    if cached is not None:
        return cached
    try:
        payload = _get_json(
            SEARCH_URL, {"term": term, "l": "german", "cc": "DE"}, delay=0.3
        )
        results = [
            {"appid": int(item["id"]), "name": item["name"]}
            for item in (payload or {}).get("items", [])
            if item.get("id") and item.get("name")
        ]
    except Exception:
        results = []
    _put_cached(session, key, results, days=7)
    return results


def _merge_unique(*lists: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for values in lists:
        for value in values:
            cleaned = (value or "").strip()
            key = cleaned.lower()
            if cleaned and key not in seen:
                seen.add(key)
                result.append(cleaned)
    return result


def enrich_game(session: Session, game: Game, force: bool = False) -> Game:
    """Fill genres, tags, review score and price from Steam/SteamSpy."""
    if not game.steam_appid:
        return game
    if (
        not force
        and game.metadata_fetched_at is not None
        and utcnow() - game.metadata_fetched_at < timedelta(days=CACHE_DAYS)
        and game.tags
    ):
        return game

    details = fetch_appdetails(session, game.steam_appid, force=force)
    if details:
        game.title = details.get("name") or game.title
        game.release_date = (details.get("release_date") or {}).get("date") or game.release_date
        game.genres = [g.get("description") for g in details.get("genres", []) if g.get("description")]
        game.platforms = [k for k, v in (details.get("platforms") or {}).items() if v]
        game.developers = details.get("developers") or []
        game.publishers = details.get("publishers") or []
        game.header_image = details.get("header_image") or game.header_image
        game.short_description = (details.get("short_description") or "")[:1200] or game.short_description
        price = details.get("price_overview") or {}
        if price:
            game.price_cents = price.get("final")
            game.currency = price.get("currency")
        elif details.get("is_free"):
            game.price_cents = 0

    steamspy = fetch_steamspy(session, game.steam_appid, force=force)
    if steamspy:
        tags = steamspy.get("tags") or {}
        if isinstance(tags, dict):
            top_tags = [
                name
                for name, _votes in sorted(
                    tags.items(), key=lambda item: item[1] if isinstance(item[1], int) else 0, reverse=True
                )[:20]
            ]
            game.tags = _merge_unique(game.tags, top_tags)

    reviews = fetch_review_summary(session, game.steam_appid, force=force)
    if reviews:
        positive = int(reviews.get("total_positive") or 0)
        negative = int(reviews.get("total_negative") or 0)
        total = int(reviews.get("total_reviews") or (positive + negative))
        if total and positive + negative:
            game.review_score = int(round(100 * positive / (positive + negative)))
        game.review_count = total
        game.review_desc = reviews.get("review_score_desc")

    game.metadata_fetched_at = utcnow()
    game.metadata_json = {
        "appdetails": {k: details.get(k) for k in ("required_age", "content_descriptors", "categories")}
        if details
        else {},
        "steamspy_owners": (steamspy or {}).get("owners"),
        "steamspy_average_forever": (steamspy or {}).get("average_forever"),
    }
    session.commit()
    return game


def popularity_score(game: Game) -> float:
    if not game.review_count:
        return 0.0
    return min(1.0, math.log10(max(game.review_count, 1)) / 6.0)
