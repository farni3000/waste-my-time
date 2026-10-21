from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Game, LibraryEntry, PlaytimeSnapshot, Setting, SteamAccount
from ..timeutil import utcnow
from .http import USER_AGENT, polite_delay
from .store import enrich_game

API_BASE = "https://api.steampowered.com"
Progress = Callable[[int, str], None] | None


def get_steam_id(session: Session) -> str:
    configured = session.get(Setting, "steam_id")
    value = (configured.value if configured else "") or get_settings().steam_id
    return value.strip()


def set_steam_id(session: Session, value: str) -> None:
    setting = session.get(Setting, "steam_id")
    if setting is None:
        session.add(Setting(key="steam_id", value=value.strip()))
    else:
        setting.value = value.strip()
    session.commit()


def _api_get(path: str, params: dict[str, Any]) -> dict:
    polite_delay(API_BASE + path, 0.2)
    payload = {
        "key": get_settings().steam_api_key,
        "format": "json",
        **params,
    }
    response = httpx.get(
        f"{API_BASE}{path}",
        params=payload,
        timeout=get_settings().request_timeout,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return response.json().get("response", {})


def resolve_steam_id(session: Session) -> str:
    value = get_steam_id(session)
    if not value:
        raise ValueError("Kein SteamID konfiguriert. Bitte in den Einstellungen oder .env setzen.")
    if re.fullmatch(r"\d{17}", value):
        return value
    data = _api_get("/ISteamUser/ResolveVanityURL/v1/", {"vanityurl": value})
    if data.get("success") != 1 or not data.get("steamid"):
        raise ValueError(f"Steam-Profil '{value}' konnte nicht aufgelöst werden.")
    return str(data["steamid"])


def fetch_player_summary(steamid64: str) -> dict:
    data = _api_get("/ISteamUser/GetPlayerSummaries/v2/", {"steamids": steamid64})
    players = data.get("players") or []
    return players[0] if players else {}


def fetch_owned_games(steamid64: str) -> list[dict]:
    data = _api_get(
        "/IPlayerService/GetOwnedGames/v1/",
        {
            "steamid": steamid64,
            "include_appinfo": 1,
            "include_played_free_games": 1,
            "include_free_sub": 1,
        },
    )
    games = data.get("games")
    if games is None:
        raise ValueError(
            "Steam hat keine Spiele geliefert. Ist dein Profil öffentlich "
            "und sind die Spieldetails sichtbar?"
        )
    return list(games)


def _upsert_game(session: Session, appid: int, title: str) -> Game:
    game = session.scalar(select(Game).where(Game.steam_appid == appid))
    if game is None:
        from ..domain.normalize import normalize_title

        game = Game(
            steam_appid=appid,
            title=title,
            normalized_title=normalize_title(title),
            source_primary="steam",
        )
        session.add(game)
        session.flush()
    elif title and game.title != title:
        game.title = title
    return game


def sync_library(
    session: Session,
    enrich: bool = True,
    progress: Progress = None,
) -> dict:
    if not get_settings().steam_api_key:
        raise ValueError("STEAM_API_KEY fehlt in .env.")

    def report(percent: int, message: str) -> None:
        if progress:
            progress(percent, message)

    report(2, "SteamID auflösen")
    steamid64 = resolve_steam_id(session)
    report(6, "Spielbibliothek laden")
    owned = fetch_owned_games(steamid64)
    if not owned:
        raise ValueError("Die Steam-Bibliothek ist leer oder nicht öffentlich.")

    summary = fetch_player_summary(steamid64)
    account = session.scalar(select(SteamAccount).where(SteamAccount.steamid64 == steamid64))
    if account is None:
        account = SteamAccount(steamid64=steamid64)
        session.add(account)
    account.persona_name = summary.get("personaname") or account.persona_name
    account.avatar_url = summary.get("avatarfull") or account.avatar_url
    account.last_sync_at = utcnow()
    session.commit()

    snapshots = 0
    total = len(owned)
    for index, item in enumerate(owned, start=1):
        appid = int(item["appid"])
        title = item.get("name") or f"App {appid}"
        minutes = int(item.get("playtime_forever") or 0)
        game = _upsert_game(session, appid, title)

        entry = session.scalar(
            select(LibraryEntry).where(
                LibraryEntry.game_id == game.id,
                LibraryEntry.platform == "pc",
            )
        )
        if entry is None:
            entry = LibraryEntry(game_id=game.id, platform="pc", source="steam")
            session.add(entry)
        entry.minutes_played = minutes
        if entry.status not in {"completed", "abandoned"}:
            entry.status = "playing" if minutes > 0 else "owned"
        played_at = int(item.get("rtime_last_played") or 0)
        if played_at:
            entry.last_played_at = datetime.fromtimestamp(played_at, UTC).replace(tzinfo=None)

        latest = session.scalar(
            select(PlaytimeSnapshot)
            .where(PlaytimeSnapshot.steam_appid == appid)
            .order_by(desc(PlaytimeSnapshot.captured_at))
            .limit(1)
        )
        if latest is None or latest.minutes != minutes:
            session.add(PlaytimeSnapshot(steam_appid=appid, game_id=game.id, minutes=minutes))
            snapshots += 1

        if index % 100 == 0:
            session.commit()
            report(6 + int(34 * index / total), f"{index}/{total} Spiele importiert")
    session.commit()
    report(42, f"{total} Spiele importiert, {snapshots} Spielzeit-Snapshots")

    enriched = 0
    if enrich:
        limit = get_settings().steam_metadata_limit
        games = session.scalars(
            select(Game)
            .join(LibraryEntry, LibraryEntry.game_id == Game.id)
            .where(Game.steam_appid.is_not(None))
            .order_by(desc(LibraryEntry.minutes_played))
            .limit(limit)
        ).all()
        for index, game in enumerate(games, start=1):
            try:
                enrich_game(session, game)
                enriched += 1
            except Exception:
                session.rollback()
            if index % 5 == 0:
                report(42 + int(56 * index / max(len(games), 1)), f"Metadaten {index}/{len(games)}")
        report(99, "Metadaten aktualisiert")

    return {
        "steamid64": steamid64,
        "games": total,
        "snapshots": snapshots,
        "enriched": enriched,
    }


def enrich_library(session: Session, limit: int = 80, progress: Progress = None) -> dict:
    games = session.scalars(
        select(Game)
        .join(LibraryEntry, LibraryEntry.game_id == Game.id)
        .where(Game.steam_appid.is_not(None))
        .order_by(desc(LibraryEntry.minutes_played))
        .limit(limit)
    ).all()
    enriched = 0
    for index, game in enumerate(games, start=1):
        try:
            enrich_game(session, game)
            enriched += 1
        except Exception:
            session.rollback()
        if progress and index % 5 == 0:
            progress(int(100 * index / max(len(games), 1)), f"Metadaten {index}/{len(games)}")
    return {"enriched": enriched, "checked": len(games)}
