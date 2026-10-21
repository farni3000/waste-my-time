from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.normalize import normalize_title
from ..models import Game, GameAlias, Mention, SourceItem
from ..timeutil import utcnow

_RANK_PREFIX = re.compile(r"^\s*(?:#|\d+[\.\)\:])\s*")


@dataclass
class CatalogEntry:
    game_id: int
    title: str
    normalized: str


def build_catalog(session: Session) -> list[CatalogEntry]:
    entries: dict[str, CatalogEntry] = {}
    for game in session.scalars(select(Game)):
        normalized = game.normalized_title or normalize_title(game.title)
        if normalized and normalized not in entries:
            entries[normalized] = CatalogEntry(game.id, game.title, normalized)
    for alias in session.scalars(select(GameAlias)):
        if alias.normalized_alias and alias.normalized_alias not in entries:
            entries[alias.normalized_alias] = CatalogEntry(
                alias.game_id, alias.alias, alias.normalized_alias
            )
    return sorted(entries.values(), key=lambda entry: len(entry.normalized), reverse=True)


def _clean_raw_title(raw: str) -> str:
    value = _RANK_PREFIX.sub("", raw or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:300]


def create_mention(
    session: Session,
    item: SourceItem,
    raw_title: str,
    game_id: int | None,
    rank: int | None = None,
    confidence: float = 1.0,
    resolved_by: str = "auto",
    status: str = "resolved",
    commit: bool = False,
) -> Mention | None:
    if game_id is not None:
        existing = session.scalar(
            select(Mention).where(
                Mention.source_item_id == item.id,
                Mention.game_id == game_id,
            )
        )
        if existing:
            return existing
    mention = Mention(
        source_item_id=item.id,
        raw_title=_clean_raw_title(raw_title),
        game_id=game_id,
        rank=rank,
        confidence=confidence,
        resolved_by=resolved_by,
        status=status,
    )
    session.add(mention)
    if commit:
        session.commit()
    return mention


def extract_known_titles(
    session: Session,
    item: SourceItem,
    text: str,
    catalog: list[CatalogEntry] | None = None,
) -> list[Game]:
    """Find known game titles in free text (RSS titles/summaries)."""
    if not text:
        return []
    catalog = catalog or build_catalog(session)
    normalized_text = " " + normalize_title(text) + " "
    found: list[Game] = []
    seen_ids: set[int] = set()
    for entry in catalog:
        if len(entry.normalized) < 4 or entry.game_id in seen_ids:
            continue
        needle = " " + entry.normalized + " "
        position = normalized_text.find(needle)
        if position < 0:
            continue
        found.append(session.get(Game, entry.game_id))
        seen_ids.add(entry.game_id)
        normalized_text = (
            normalized_text[: position]
            + " " * len(needle)
            + normalized_text[position + len(needle) :]
        )
        if len(found) >= 8:
            break
    for game in found:
        if game is None:
            continue
        create_mention(
            session,
            item,
            game.title,
            game.id,
            rank=None,
            confidence=0.9,
            resolved_by="auto",
            status="resolved",
        )
    if found:
        session.commit()
    return [game for game in found if game is not None]


def suggestions(session: Session, raw_title: str, limit: int = 3) -> list[tuple[Game, float]]:
    catalog = build_catalog(session)
    if not catalog:
        return []
    normalized = normalize_title(_clean_raw_title(raw_title))
    if not normalized:
        return []
    choices = {entry.normalized: entry for entry in catalog}
    matches = process.extract(
        normalized,
        list(choices.keys()),
        scorer=fuzz.WRatio,
        limit=limit,
        score_cutoff=55,
    )
    result: list[tuple[Game, float]] = []
    for match in matches:
        key = match[0]
        score = match[1]
        entry = choices[key]
        game = session.get(Game, entry.game_id)
        if game is not None:
            result.append((game, round(score, 1)))
    return result


def resolve_list_entry(
    session: Session,
    item: SourceItem,
    raw_title: str,
    rank: int | None = None,
    catalog: list[CatalogEntry] | None = None,
) -> Mention:
    """Resolve a whole list entry (best-of list) to a game or queue it for review."""
    cleaned = _clean_raw_title(raw_title)
    normalized = normalize_title(cleaned)
    catalog = catalog or build_catalog(session)
    if not normalized:
        return create_mention(
            session, item, cleaned, None, rank, 0.0, "auto", "pending", commit=True
        )

    choices = {entry.normalized: entry for entry in catalog}
    if normalized in choices:
        entry = choices[normalized]
        return create_mention(
            session, item, cleaned, entry.game_id, rank, 1.0, "auto", "resolved", commit=True
        )

    if choices:
        matches = process.extract(
            normalized, list(choices.keys()), scorer=fuzz.WRatio, limit=2, score_cutoff=60
        )
    else:
        matches = []
    if matches:
        best_key = matches[0][0]
        best_score = matches[0][1]
        second_score = matches[1][1] if len(matches) > 1 else 0
        if best_score >= 92 and best_score - second_score >= 3:
            entry = choices[best_key]
            return create_mention(
                session,
                item,
                cleaned,
                entry.game_id,
                rank,
                round(best_score / 100, 2),
                "auto",
                "resolved",
                commit=True,
            )
    return create_mention(
        session, item, cleaned, None, rank, 0.0, "auto", "pending", commit=True
    )


def create_game_from_mention(
    session: Session,
    mention: Mention,
    title: str | None = None,
    lookup_store: bool = True,
) -> Game | None:
    from ..sources.store import enrich_game, search_store

    cleaned = _clean_raw_title(title or mention.raw_title)
    if not cleaned:
        return None
    normalized = normalize_title(cleaned)
    game = session.scalar(select(Game).where(Game.normalized_title == normalized))
    if game is None:
        game = Game(
            title=cleaned,
            normalized_title=normalized,
            source_primary="editorial",
        )
        session.add(game)
        session.flush()
    session.commit()

    if lookup_store and game.steam_appid is None:
        game_id = game.id
        try:
            results = search_store(session, cleaned)
            if results:
                best = results[0]
                game.steam_appid = best["appid"]
                game.title = best["name"]
                game.normalized_title = normalize_title(best["name"])
                session.commit()
                enrich_game(session, game)
        except Exception:
            session.rollback()
            game = session.get(Game, game_id)

    mention.game_id = game.id
    mention.status = "resolved"
    mention.resolved_by = "manual"
    session.commit()
    return game


def ignore_mention(session: Session, mention: Mention) -> None:
    mention.status = "ignored"
    session.commit()


def resolve_pending(session: Session, limit: int = 200) -> dict:
    """Retry auto-resolution for pending mentions."""
    pending = session.scalars(
        select(Mention).where(Mention.status == "pending").limit(limit)
    ).all()
    resolved = 0
    for mention in pending:
        options = suggestions(session, mention.raw_title, limit=2)
        if options and options[0][1] >= 92 and (
            len(options) == 1 or options[0][1] - options[1][1] >= 3
        ):
            mention.game_id = options[0][0].id
            mention.status = "resolved"
            mention.confidence = options[0][1] / 100
            mention.resolved_by = "auto"
            resolved += 1
    session.commit()
    return {"checked": len(pending), "resolved": resolved, "at": utcnow().isoformat()}
