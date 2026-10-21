from __future__ import annotations

import os
from pathlib import Path

os.environ["DB_PATH"] = "data/test.db"
os.environ["STEAM_API_KEY"] = "test-key"
os.environ["STEAM_ID"] = "76561197980782631"
os.environ["DEEPSEEK_API_KEY"] = "test-deepseek"

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine, run_migrations  # noqa: E402
from app.models import Game, LibraryEntry  # noqa: E402

TEST_DB = Path(__file__).resolve().parent.parent / "data" / "test.db"


@pytest.fixture(scope="session", autouse=True)
def prepared_database():
    TEST_DB.unlink(missing_ok=True)
    run_migrations()
    yield
    engine.dispose()
    TEST_DB.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(TEST_DB) + suffix).unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def clean_database():
    yield
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())


@pytest.fixture
def session():
    with SessionLocal() as db:
        yield db


def make_game(
    session,
    title: str,
    tags: list[str] | None = None,
    genres: list[str] | None = None,
    steam_appid: int | None = None,
) -> Game:
    from app.domain.normalize import normalize_title

    game = Game(
        title=title,
        normalized_title=normalize_title(title),
        tags=tags or [],
        genres=genres or [],
        steam_appid=steam_appid,
        source_primary="steam" if steam_appid else "manual",
    )
    session.add(game)
    session.flush()
    return game


def make_entry(
    session,
    game: Game,
    hours: float = 10,
    rating: int | None = None,
    status: str = "playing",
    source: str = "steam",
) -> LibraryEntry:
    entry = LibraryEntry(
        game_id=game.id,
        platform="pc",
        source=source,
        status=status,
        minutes_played=int(hours * 60),
        rating=rating,
    )
    session.add(entry)
    session.commit()
    return entry
