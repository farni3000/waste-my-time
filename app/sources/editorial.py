from __future__ import annotations

import hashlib
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Source, SourceItem
from ..timeutil import utcnow
from .http import fetch_text
from .resolve import build_catalog, resolve_list_entry


def fetch_editorial(session: Session, source: Source, max_items: int = 60) -> dict:
    config = source.config_json or {}
    item_selector = config.get("item_selector") or "article, li"
    title_selector = config.get("title_selector") or ""
    max_items = int(config.get("max_items") or max_items)
    ranked = bool(config.get("ranked", True))

    text = fetch_text(source.url)
    soup = BeautifulSoup(text, "lxml")
    items = soup.select(item_selector)
    if not items:
        raise ValueError(
            f"Keine Listeneinträge für Selektor '{item_selector}' gefunden. "
            "Bitte die CSS-Selektoren in der Quelle anpassen."
        )

    catalog = build_catalog(session)
    new_items = 0
    mentions = 0
    for index, node in enumerate(items[:max_items], start=1):
        title_node = node.select_one(title_selector) if title_selector else node
        if title_node is None:
            continue
        raw_title = " ".join(title_node.get_text(" ", strip=True).split())
        if len(raw_title) < 2 or len(raw_title) > 250:
            continue
        link_node = node.select_one("a") or (title_node if title_node.name == "a" else None)
        href = link_node.get("href") if link_node is not None else None
        url = urljoin(source.url, href) if href else f"{source.url}#{index}"
        if session.scalar(select(SourceItem).where(SourceItem.url == url)) is not None:
            continue
        digest = hashlib.sha1(raw_title.encode("utf-8")).hexdigest()
        item = SourceItem(
            source_id=source.id,
            url=url,
            title=raw_title,
            excerpt=raw_title,
            content_hash=digest,
            raw_json={"rank": index},
        )
        session.add(item)
        session.flush()
        new_items += 1
        resolve_list_entry(session, item, raw_title, rank=index if ranked else None, catalog=catalog)
        mentions += 1

    source.last_fetched_at = utcnow()
    source.last_error = None
    session.commit()
    return {"source": source.name, "new_items": new_items, "mentions": mentions}
