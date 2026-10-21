from __future__ import annotations

import hashlib
import json
import math

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import Game, Interest, LibraryEntry, Recommendation, TasteProfile
from ..timeutil import utcnow

_RATING_WEIGHT = {5: 1.6, 4: 1.3, 3: 1.0, 2: -1.0, 1: -1.5}


def _unique_tags(*values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tags in values:
        for tag in tags or []:
            cleaned = (tag or "").strip()
            key = cleaned.lower()
            if cleaned and key not in seen:
                seen.add(key)
                result.append(cleaned)
    return result


def _entry_tags(entry: LibraryEntry) -> list[str]:
    return _unique_tags(entry.game.tags, entry.game.genres, entry.user_tags)


def _top(values: list[str], limit: int = 6) -> list[str]:
    return values[:limit]


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    positives = {key: value for key, value in weights.items() if value > 0}
    negatives = {key: value for key, value in weights.items() if value < 0}
    max_pos = max(positives.values(), default=0)
    max_neg = abs(min(negatives.values(), default=0))
    result = {}
    for key, value in weights.items():
        if value > 0 and max_pos:
            result[key] = round(value / max_pos, 3)
        elif value < 0 and max_neg:
            result[key] = round(value / max_neg, 3)
    return result


def build_profile(session: Session, force: bool = False) -> TasteProfile:
    entries = session.scalars(
        select(LibraryEntry).join(Game).where(LibraryEntry.status != "wishlist")
    ).all()
    interests = session.scalars(select(Interest)).all()

    affinity: dict[str, float] = {}
    total_minutes = 0
    backlog = 0
    completed = 0
    for entry in entries:
        total_minutes += entry.minutes_played or 0
        if (entry.minutes_played or 0) < 60:
            backlog += 1
        if entry.status == "completed":
            completed += 1
        tags = _entry_tags(entry)
        if not tags:
            continue
        hours = (entry.minutes_played or 0) / 60
        if hours < 1 and not entry.rating and entry.status != "completed":
            continue
        base = math.log1p(min(hours, 500))
        multiplier = _RATING_WEIGHT.get(entry.rating or 0, 1.0)
        if entry.status == "abandoned":
            multiplier -= 1.2
        elif entry.status == "completed":
            multiplier += 0.3
        evidence = base * multiplier
        for tag in tags:
            affinity[tag] = affinity.get(tag, 0.0) + evidence

    hard_filters: list[dict] = []
    notes: list[str] = []
    for interest in interests:
        if interest.kind == "note":
            notes.append(interest.value)
            continue
        if interest.hard_filter:
            hard_filters.append({"kind": interest.kind, "value": interest.value})
        affinity[interest.value] = affinity.get(interest.value, 0.0) + (interest.weight / 3) * 1.5

    feedback_by_game: dict[int, tuple[str, float]] = {}
    for recommendation in session.scalars(
        select(Recommendation).where(Recommendation.status.in_(["liked", "played", "dismissed"]))
    ):
        weight = {"liked": 0.3, "played": 0.45, "dismissed": -0.3}.get(recommendation.status, 0.0)
        previous = feedback_by_game.get(recommendation.game_id)
        if previous is None or abs(weight) > abs(previous[1]):
            feedback_by_game[recommendation.game_id] = (recommendation.status, weight)
    for game_id, (_status, weight) in feedback_by_game.items():
        game = session.get(Game, game_id)
        if game is None:
            continue
        for tag in _unique_tags(game.tags, game.genres):
            affinity[tag] = affinity.get(tag, 0.0) + weight

    normalized = _normalize_weights(affinity)
    loved_tags = [
        {"tag": tag, "weight": weight}
        for tag, weight in sorted(normalized.items(), key=lambda item: item[1], reverse=True)
        if weight >= 0.08
    ][:15]
    disliked_tags = [
        {"tag": tag, "weight": weight}
        for tag, weight in sorted(normalized.items(), key=lambda item: item[1])
        if weight <= -0.08
    ][:10]

    def entry_payload(entry: LibraryEntry) -> dict:
        return {
            "title": entry.game.title,
            "hours": round((entry.minutes_played or 0) / 60, 1),
            "rating": entry.rating,
            "status": entry.status,
            "source": entry.source,
            "tags": _top(_entry_tags(entry), 6),
        }

    by_playtime = sorted(entries, key=lambda entry: entry.minutes_played or 0, reverse=True)
    top_games = [entry_payload(entry) for entry in by_playtime[:12] if (entry.minutes_played or 0) > 0]

    recent = sorted(
        [entry for entry in entries if entry.last_played_at is not None],
        key=lambda entry: entry.last_played_at,
        reverse=True,
    )[:10]
    recent_games = [
        {
            **entry_payload(entry),
            "days_ago": max(0, (utcnow() - entry.last_played_at).days),
        }
        for entry in recent
    ]

    loved = [
        entry_payload(entry)
        for entry in entries
        if entry.favorite or (entry.rating or 0) >= 4
    ][:10]
    abandoned = [
        entry_payload(entry)
        for entry in entries
        if entry.status == "abandoned" or (entry.rating is not None and entry.rating <= 2)
    ][:10]
    manual_favorites = [
        entry_payload(entry)
        for entry in entries
        if entry.source == "manual" and (entry.favorite or (entry.rating or 0) >= 4)
    ][:10]

    summary = {
        "generated_at": utcnow().isoformat(timespec="seconds"),
        "totals": {
            "hours": round(total_minutes / 60, 1),
            "games": len(entries),
            "backlog": backlog,
            "completed": completed,
        },
        "top_games": top_games,
        "recent_games": recent_games,
        "loved_games": loved,
        "abandoned_games": abandoned,
        "manual_favorites": manual_favorites,
        "loved_tags": loved_tags,
        "disliked_tags": disliked_tags,
        "constraints": hard_filters,
        "notes": notes,
    }

    raw_entries = [
        {
            "id": entry.game_id,
            "minutes": entry.minutes_played,
            "rating": entry.rating,
            "status": entry.status,
            "source": entry.source,
            "last_played_at": entry.last_played_at.isoformat() if entry.last_played_at else None,
            "tags": _entry_tags(entry),
        }
        for entry in sorted(entries, key=lambda entry: entry.game_id)
    ]
    raw_interests = [
        {
            "kind": interest.kind,
            "value": interest.value,
            "weight": interest.weight,
            "hard": interest.hard_filter,
        }
        for interest in sorted(interests, key=lambda interest: interest.id)
    ]
    raw_feedback = [
        {"game_id": game_id, "status": status}
        for game_id, (status, _weight) in sorted(feedback_by_game.items())
    ]
    digest = hashlib.sha1(
        json.dumps([raw_entries, raw_interests, raw_feedback], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()

    active = session.scalar(select(TasteProfile).where(TasteProfile.is_active.is_(True)))
    if active is not None and active.source_hash == digest and not force:
        return active

    version = (session.scalar(select(func.max(TasteProfile.version))) or 0) + 1
    session.execute(update(TasteProfile).values(is_active=False))
    profile = TasteProfile(
        version=version,
        summary_json=summary,
        source_hash=digest,
        is_active=True,
    )
    session.add(profile)
    session.commit()
    return profile


def active_profile(session: Session) -> TasteProfile | None:
    return session.scalar(select(TasteProfile).where(TasteProfile.is_active.is_(True)))
