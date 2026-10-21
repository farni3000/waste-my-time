from __future__ import annotations

import calendar
import hashlib
from datetime import UTC, datetime

import feedparser
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Source, SourceItem
from ..timeutil import utcnow
from .http import fetch_text
from .resolve import build_catalog, extract_known_titles


def _clean_html(value: str | None) -> str:
    if not value:
        return ""
    return BeautifulSoup(value, "lxml").get_text(" ", strip=True)


def _published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), UTC).replace(tzinfo=None)


def fetch_rss(session: Session, source: Source, max_items: int = 80) -> dict:
    text = fetch_text(source.url)
    feed = feedparser.parse(text)
    if getattr(feed, "bozo", False) and not feed.entries:
        raise ValueError(f"Feed konnte nicht gelesen werden: {source.url}")

    catalog = build_catalog(session)
    new_items = 0
    mentions = 0
    for entry in feed.entries[:max_items]:
        link = entry.get("link") or entry.get("id")
        if not link:
            continue
        existing = session.scalar(select(SourceItem).where(SourceItem.url == link))
        if existing is not None:
            continue
        title = (entry.get("title") or "Ohne Titel")[:400]
        summary = _clean_html(entry.get("summary") or entry.get("description"))[:4000]
        excerpt = summary[:300]
        digest = hashlib.sha1(f"{title}{summary}".encode()).hexdigest()
        item = SourceItem(
            source_id=source.id,
            url=link,
            title=title,
            published_at=_published(entry),
            excerpt=excerpt,
            content_hash=digest,
            raw_json={"feed": getattr(feed.feed, "title", None)},
        )
        session.add(item)
        session.flush()
        new_items += 1
        before = len(extract_known_titles(session, item, f"{title} {summary}", catalog))
        mentions += before

    source.last_fetched_at = utcnow()
    source.last_error = None
    session.commit()
    return {"source": source.name, "new_items": new_items, "mentions": mentions}
