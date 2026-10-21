from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..models import Game, LibraryEntry, Mention, SourceItem, TasteProfile
from ..sources.store import enrich_game, fetch_steamspy_tag, popularity_score
from ..timeutil import utcnow
from .normalize import normalize_title, title_tokens


@dataclass
class Candidate:
    game: Game
    score: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)


def _weights(profile: TasteProfile) -> dict[str, float]:
    summary = profile.summary_json or {}
    weights: dict[str, float] = {}
    for item in summary.get("loved_tags", []):
        weights[item["tag"].lower()] = item["weight"]
    for item in summary.get("disliked_tags", []):
        weights[item["tag"].lower()] = item["weight"]
    return weights


def _game_tags(game: Game, extra: list[str] | None = None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tag in list(game.tags or []) + list(game.genres or []) + list(extra or []):
        cleaned = (tag or "").strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            result.append(cleaned)
    return result


def tag_affinity(profile: TasteProfile, tags: list[str]) -> float:
    weights = _weights(profile)
    values = [weights[tag.lower()] for tag in tags if tag.lower() in weights]
    if not values:
        return 0.2
    average = sum(values) / len(values)
    return max(0.0, min(1.0, (average + 1) / 2))


def similarity(profile: TasteProfile, tags: list[str]) -> float:
    tag_set = {tag.lower() for tag in tags}
    if not tag_set:
        return 0.0
    scores: list[float] = []
    for game in (profile.summary_json or {}).get("top_games", [])[:6]:
        other = {tag.lower() for tag in game.get("tags", [])}
        if not other:
            continue
        intersection = len(tag_set & other)
        union = len(tag_set | other)
        scores.append(intersection / union if union else 0.0)
    if not scores:
        return 0.0
    scores.sort(reverse=True)
    return sum(scores[:3]) / min(3, len(scores))


def editorial_signal(session: Session, game_id: int) -> float:
    rows = session.execute(
        select(Mention, SourceItem)
        .join(SourceItem, SourceItem.id == Mention.source_item_id)
        .where(Mention.game_id == game_id, Mention.status == "resolved")
    ).all()
    if not rows:
        return 0.0
    best_per_source: dict[int, float] = {}
    for mention, item in rows:
        published = item.published_at or item.fetched_at or utcnow()
        days = max(0, (utcnow() - published).days)
        decay = math.exp(-days / 120)
        rank_factor = 1.0 if not mention.rank else max(0.25, 1 - mention.rank / 60)
        value = decay * rank_factor * max(0.4, mention.confidence)
        source_id = item.source_id
        best_per_source[source_id] = max(best_per_source.get(source_id, 0.0), value)
    total = sum(best_per_source.values())
    return min(1.0, total / 2.0)


def novelty(profile: TasteProfile, game: Game) -> float:
    tokens = title_tokens(game.title)
    if not tokens:
        return 0.5
    for recent in (profile.summary_json or {}).get("recent_games", [])[:8]:
        recent_tokens = title_tokens(recent.get("title", ""))
        if not recent_tokens:
            continue
        union = len(tokens | recent_tokens)
        if union and len(tokens & recent_tokens) / union > 0.6:
            return 0.0
    return 1.0


def _jitter(game_id: int) -> float:
    digest = hashlib.sha1(str(game_id).encode("utf-8")).hexdigest()[:8]
    return (int(digest, 16) % 1000) / 1000 * 0.02


_SERIES_STOP = {"the", "a", "an", "of", "and", "in", "to", "edition", "remastered", "touch"}


def series_key(title: str) -> str:
    """Rough franchise key, e.g. 'Football Manager 2020 Touch' -> 'football manager'."""
    parts = [
        part
        for part in normalize_title(title).split()
        if part not in _SERIES_STOP and not part.isdigit() and len(part) > 2
    ]
    return " ".join(parts[:2]) or normalize_title(title)


def _hard_exclusions(filters: dict, profile: TasteProfile | None = None) -> set[str]:
    raw = filters.get("exclude_tags") or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    excluded = {tag.lower() for tag in raw if tag}
    if profile is not None:
        for constraint in (profile.summary_json or {}).get("constraints", []):
            kind = (constraint.get("kind") or "").lower()
            value = (constraint.get("value") or "").strip()
            if not value:
                continue
            lowered = value.lower()
            if lowered.startswith(("kein ", "keine ", "no ")):
                excluded.add(lowered.split(" ", 1)[1])
            elif kind in {"genre", "theme", "mechanic", "tag"}:
                excluded.add(lowered)
    return excluded


def _owned_appids(session: Session) -> set[int]:
    return {
        appid
        for appid in session.scalars(
            select(Game.steam_appid).join(LibraryEntry, LibraryEntry.game_id == Game.id)
        )
        if appid
    }


def _enrich_missing(session: Session, games: list[Game], limit: int = 20) -> None:
    done = 0
    for game in games:
        if done >= limit:
            break
        if not game.steam_appid or (game.tags and game.metadata_fetched_at):
            continue
        try:
            enrich_game(session, game)
        except Exception:
            session.rollback()
        done += 1


def backlog_candidates(
    session: Session,
    profile: TasteProfile,
    filters: dict | None = None,
    limit: int = 40,
) -> list[Candidate]:
    filters = filters or {}
    excluded = _hard_exclusions(filters, profile)
    cutoff = utcnow() - timedelta(days=14)
    entries = session.scalars(
        select(LibraryEntry)
        .join(Game)
        .where(
            LibraryEntry.status.in_(["owned", "playing"]),
            LibraryEntry.minutes_played < 180,
        )
        .order_by(desc(Game.review_score))
    ).all()
    entries = [
        entry
        for entry in entries
        if entry.last_played_at is None or entry.last_played_at < cutoff
    ]
    games = [entry.game for entry in entries]
    _enrich_missing(session, games, limit=16)

    candidates: list[Candidate] = []
    for entry in entries:
        game = entry.game
        tags = _game_tags(game, entry.user_tags)
        lowered = {tag.lower() for tag in tags}
        if excluded and lowered & excluded:
            continue
        affinity = tag_affinity(profile, tags)
        if affinity < 0.18:
            continue
        sim = similarity(profile, tags)
        editorial = editorial_signal(session, game.id)
        novel = novelty(profile, game)
        score = 0.58 * affinity + 0.22 * sim + 0.12 * editorial + 0.08 * novel
        score += _jitter(game.id)
        candidates.append(
            Candidate(
                game=game,
                score=round(score, 4),
                breakdown={
                    "affinity": round(affinity, 3),
                    "similarity": round(sim, 3),
                    "editorial": round(editorial, 3),
                    "novelty": round(novel, 3),
                },
            )
        )

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    selected: list[Candidate] = []
    per_primary: dict[str, int] = {}
    per_series: dict[str, int] = {}
    for candidate in candidates:
        primary = (candidate.game.genres or ["unbekannt"])[0].lower()
        if per_primary.get(primary, 0) >= 3:
            continue
        series = series_key(candidate.game.title)
        if per_series.get(series, 0) >= 2:
            continue
        per_primary[primary] = per_primary.get(primary, 0) + 1
        per_series[series] = per_series.get(series, 0) + 1
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


_GENERIC_TAGS = {
    "singleplayer",
    "multiplayer",
    "action",
    "adventure",
    "indie",
    "free to play",
    "early access",
    "atmospheric",
    "story rich",
    "great soundtrack",
    "massively multiplayer",
    "mmo",
    "battle royale",
    "pvp",
}


def new_candidates(
    session: Session,
    profile: TasteProfile,
    filters: dict | None = None,
    limit: int = 40,
) -> list[Candidate]:
    filters = filters or {}
    excluded = _hard_exclusions(filters, profile)
    owned = _owned_appids(session)
    weights = _weights(profile)
    loved_tags = [
        tag
        for tag, weight in sorted(weights.items(), key=lambda item: item[1], reverse=True)
        if weight > 0.2 and tag not in _GENERIC_TAGS
    ][:5]
    if not loved_tags:
        loved_tags = ["Indie"]

    pool: dict[int, dict] = {}
    for tag in loved_tags:
        for appid, name in fetch_steamspy_tag(session, tag).items():
            if appid in owned:
                continue
            entry = pool.setdefault(appid, {"title": name, "hits": set()})
            entry["hits"].add(tag)
            if len(pool) > 400:
                break

    editorial_games: list[Game] = []
    editorial_rows = session.execute(
        select(Mention.game_id, Game)
        .join(Game, Game.id == Mention.game_id)
        .where(Mention.status == "resolved")
    ).all()
    for _game_id, game in editorial_rows:
        if game.steam_appid and game.steam_appid in owned:
            continue
        if game.steam_appid:
            entry = pool.setdefault(game.steam_appid, {"title": game.title, "hits": set()})
            entry["hits"].add("__editorial__")
        elif game not in editorial_games:
            editorial_games.append(game)

    ranked_pool = sorted(pool.items(), key=lambda item: len(item[1]["hits"]), reverse=True)[:80]
    games: list[Game] = list(editorial_games)
    for appid, entry in ranked_pool:
        game = session.scalar(select(Game).where(Game.steam_appid == appid))
        if game is None:
            from .normalize import normalize_title

            game = Game(
                steam_appid=appid,
                title=entry["title"],
                normalized_title=normalize_title(entry["title"]),
                source_primary="steamspy",
                tags=sorted(entry["hits"] - {"__editorial__"}),
            )
            session.add(game)
            session.flush()
        elif not game.tags:
            game.tags = sorted(entry["hits"] - {"__editorial__"})
        games.append(game)
    session.commit()

    _enrich_missing(session, games, limit=14)

    max_price = filters.get("max_price")
    candidates: list[Candidate] = []
    for game in games:
        if game.steam_appid in owned:
            continue
        if game.review_score is not None and game.review_score < 70:
            continue
        if max_price not in (None, "", 0) and game.price_cents is not None:
            try:
                if game.price_cents / 100 > float(max_price):
                    continue
            except (TypeError, ValueError):
                pass
        tags = _game_tags(game)
        lowered = {tag.lower() for tag in tags}
        if excluded and lowered & excluded:
            continue
        affinity = tag_affinity(profile, tags)
        if affinity < 0.4:
            continue
        sim = similarity(profile, tags)
        editorial = editorial_signal(session, game.id)
        pop = popularity_score(game)
        score = 0.5 * affinity + 0.2 * sim + 0.25 * editorial + 0.05 * pop
        score += _jitter(game.id)
        candidates.append(
            Candidate(
                game=game,
                score=round(score, 4),
                breakdown={
                    "affinity": round(affinity, 3),
                    "similarity": round(sim, 3),
                    "editorial": round(editorial, 3),
                    "popularity": round(pop, 3),
                },
            )
        )

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    selected: list[Candidate] = []
    per_series: dict[str, int] = {}
    for candidate in candidates:
        if candidate.score <= 0.08:
            continue
        series = series_key(candidate.game.title)
        if per_series.get(series, 0) >= 2:
            continue
        per_series[series] = per_series.get(series, 0) + 1
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected
