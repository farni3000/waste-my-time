from fastapi.testclient import TestClient

from app.main import app
from app.models import Game, Interest, LibraryEntry


def test_pages_render_and_manual_entry_can_be_created(session):
    with TestClient(app) as client:
        for path in ["/", "/library", "/interests", "/sources", "/mentions", "/recommendations", "/stats", "/settings"]:
            response = client.get(path)
            assert response.status_code == 200, path

        response = client.post(
            "/library/new",
            data={
                "title": "Hollow Knight",
                "platform": "switch",
                "hours": "25,5",
                "rating": "5",
                "status": "completed",
                "notes": "großartig",
                "tags": "metroidvania, cozy",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "Hollow Knight" in response.text

        game = session.query(Game).filter_by(title="Hollow Knight").one()
        entry = session.query(LibraryEntry).filter_by(game_id=game.id).one()
        assert entry.minutes_played == 25 * 60 + 30
        assert entry.rating == 5

        client.post(
            "/interests",
            data={"kind": "mood", "value": "cozy", "weight": "2"},
            follow_redirects=True,
        )
        assert session.query(Interest).count() == 1


def test_recommendations_page_shows_empty_state(session):
    with TestClient(app) as client:
        response = client.get("/recommendations")
        assert response.status_code == 200
        assert "Noch kein Lauf" in response.text


def test_healthz(session):
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.json() == {"ok": True}


def test_recommendation_feedback_htmx_returns_card(session):
    from app.models import RecommendationRun
    from tests.conftest import make_game

    game = make_game(session, "Factorio", ["Automation"], steam_appid=427520)
    run = RecommendationRun(mode="backlog", status="done", model="test")
    session.add(run)
    session.flush()
    from app.models import Recommendation

    rec = Recommendation(run_id=run.id, game_id=game.id, rank=1, reason="passt", score=0.5)
    session.add(rec)
    session.commit()

    with TestClient(app) as client:
        response = client.post(
            f"/recommendations/{rec.id}/feedback",
            data={"status": "liked"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "gemerkt" in response.text
        session.expire_all()
        assert session.get(Recommendation, rec.id).status == "liked"
