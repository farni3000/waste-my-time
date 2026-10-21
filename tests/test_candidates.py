from app.domain.candidates import backlog_candidates, series_key, tag_affinity
from app.domain.profile import build_profile
from app.models import Interest
from tests.conftest import make_entry, make_game


def test_series_key_groups_annual_entries():
    assert series_key("Football Manager 2020 Touch") == "football manager"
    assert series_key("Football Manager 2019 Touch") == "football manager"
    assert series_key("Victoria II") == "victoria"


def test_backlog_candidates_respect_hard_exclusions(session):
    loved = make_game(session, "RimWorld", ["Simulation", "Colony Sim"])
    make_entry(session, loved, hours=200, rating=5)
    wanted = make_game(session, "Factorio", ["Simulation", "Automation"], steam_appid=427520)
    make_entry(session, wanted, hours=0)
    unwanted = make_game(session, "Horror Thing", ["Simulation", "Horror"], steam_appid=999)
    make_entry(session, unwanted, hours=0)
    make_entry(session, make_game(session, "Some Shooter", ["Action"]), hours=0, source="manual")

    session.add(Interest(kind="theme", value="Horror", hard_filter=True))
    session.commit()
    profile = build_profile(session, force=True)
    candidates = backlog_candidates(session, profile, filters={}, limit=10)
    ids = {candidate.game.id for candidate in candidates}
    assert wanted.id in ids
    assert unwanted.id not in ids


def test_tag_affinity_prefers_loved_tags(session):
    game = make_game(session, "Factorio", ["Automation"])
    make_entry(session, game, hours=100, rating=5)
    profile = build_profile(session, force=True)
    assert tag_affinity(profile, ["Automation"]) > tag_affinity(profile, ["Dating Sim"])
