from app.domain.profile import build_profile
from app.models import Interest, Recommendation, RecommendationRun
from tests.conftest import make_entry, make_game


def test_profile_aggregates_tags_and_constraints(session):
    strategy = make_game(session, "Europa Universalis IV", ["Grand Strategy", "Historical"])
    make_entry(session, strategy, hours=300, rating=5)
    horror = make_game(session, "Resident Evil", ["Horror", "Survival"])
    make_entry(session, horror, hours=20, rating=1)
    short = make_game(session, "Unplayed", ["Strategy"])
    make_entry(session, short, hours=0.2)

    session.add(Interest(kind="mood", value="cozy", weight=2, hard_filter=False))
    session.add(Interest(kind="constraint", value="kein Horror", weight=0, hard_filter=True))
    session.commit()

    profile = build_profile(session, force=True)
    summary = profile.summary_json

    loved = {item["tag"] for item in summary["loved_tags"]}
    disliked = {item["tag"] for item in summary["disliked_tags"]}
    assert "Grand Strategy" in loved
    assert "Horror" in disliked
    assert summary["totals"]["games"] == 3
    assert summary["totals"]["backlog"] == 1
    assert summary["constraints"] == [{"kind": "constraint", "value": "kein Horror"}]
    assert any(note == "cozy" for note in summary["notes"]) is False
    assert "cozy" in loved


def test_profile_is_not_rebuilt_without_changes(session):
    game = make_game(session, "RimWorld", ["Simulation", "Colony Sim"])
    make_entry(session, game, hours=50)
    first = build_profile(session, force=True)
    second = build_profile(session)
    assert first.id == second.id
    assert first.version == second.version


def test_feedback_contributing_tags_are_included(session):
    game = make_game(session, "Hades", ["Roguelike", "Action"])
    make_entry(session, game, hours=1)
    run = RecommendationRun(mode="backlog")
    session.add(run)
    session.flush()
    session.add(
        Recommendation(run_id=run.id, game_id=game.id, status="liked", reason="test")
    )
    session.commit()

    profile = build_profile(session, force=True)
    loved = {item["tag"] for item in profile.summary_json["loved_tags"]}
    assert "Roguelike" in loved
