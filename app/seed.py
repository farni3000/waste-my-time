from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Source

DEFAULT_SOURCES = [
    ("Rock Paper Shotgun", "rss", "https://www.rockpapershotgun.com/feed"),
    ("PC Gamer", "rss", "https://www.pcgamer.com/rss/"),
    ("Eurogamer", "rss", "https://www.eurogamer.net/feed"),
    ("Polygon", "rss", "https://www.polygon.com/rss/index.xml"),
]


def seed_default_sources(session: Session) -> int:
    existing = {
        url for url in session.scalars(select(Source.url))
    }
    created = 0
    for name, kind, url in DEFAULT_SOURCES:
        if url in existing:
            continue
        session.add(Source(name=name, kind=kind, url=url))
        created += 1
    if created:
        session.commit()
    return created
