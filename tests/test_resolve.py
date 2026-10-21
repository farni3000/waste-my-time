from app.models import Mention, Source, SourceItem
from app.sources.resolve import (
    build_catalog,
    extract_known_titles,
    resolve_list_entry,
    suggestions,
)
from tests.conftest import make_game


def _source_item(session, url="https://example.org/list"):
    source = Source(name="Test", kind="html", url="https://example.org")
    session.add(source)
    session.flush()
    item = SourceItem(
        source_id=source.id,
        url=url,
        title="Test list",
        excerpt="",
        content_hash="x",
    )
    session.add(item)
    session.commit()
    return item


def test_extract_known_titles_finds_longest_match(session):
    make_game(session, "Half-Life")
    make_game(session, "Half-Life 2")
    item = _source_item(session)
    text = "Half-Life 2 ist besser als Half-Life."
    found = extract_known_titles(session, item, text)
    assert {game.title for game in found} == {"Half-Life 2", "Half-Life"}


def test_resolve_list_entry_resolves_exact_and_queues_unknown(session):
    make_game(session, "Hollow Knight")
    item = _source_item(session)
    resolved = resolve_list_entry(session, item, "Hollow Knight", rank=1)
    assert resolved.status == "resolved"
    unknown = resolve_list_entry(session, item, "Unbekanntes Spiel XYZ", rank=2)
    assert unknown.status == "pending"
    assert unknown.game_id is None


def test_suggestions_return_close_match(session):
    make_game(session, "Hollow Knight")
    _source_item(session)
    suggestion = suggestions(session, "Hollow Knigth")
    assert suggestion
    assert suggestion[0][0].title == "Hollow Knight"
    assert build_catalog(session)


def test_mentions_page_shows_pending_with_suggestions(session):
    from fastapi.testclient import TestClient

    from app.main import app

    make_game(session, "Hollow Knight")
    item = _source_item(session, url="https://example.org/best-of")
    session.add(
        Mention(
            source_item_id=item.id,
            raw_title="Hollow Knigth",
            status="pending",
            rank=1,
        )
    )
    session.commit()

    with TestClient(app) as client:
        response = client.get("/mentions")
        assert response.status_code == 200
        assert "Hollow Knigth" in response.text
        assert "Hollow Knight" in response.text
