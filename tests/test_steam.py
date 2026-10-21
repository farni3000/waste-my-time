import httpx
import respx

from app.sources.steam import fetch_owned_games, sync_library


@respx.mock
def test_fetch_owned_games_parses_response(session):
    respx.get(url__regex=r".*GetOwnedGames.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "game_count": 2,
                    "games": [
                        {"appid": 10, "name": "Game A", "playtime_forever": 120},
                        {"appid": 20, "name": "Game B", "playtime_forever": 0},
                    ],
                }
            },
        )
    )
    games = fetch_owned_games("76561197980782631")
    assert len(games) == 2
    assert games[0]["name"] == "Game A"


@respx.mock
def test_sync_library_creates_entries_and_snapshots(session):
    respx.get(url__regex=r".*GetOwnedGames.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "game_count": 2,
                    "games": [
                        {
                            "appid": 10,
                            "name": "Game A",
                            "playtime_forever": 120,
                            "rtime_last_played": 1700000000,
                        },
                        {"appid": 20, "name": "Game B", "playtime_forever": 0},
                    ],
                }
            },
        )
    )
    respx.get(url__regex=r".*GetPlayerSummaries.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "players": [
                        {"personaname": "Tester", "avatarfull": "https://example.org/a.jpg"}
                    ]
                }
            },
        )
    )

    result = sync_library(session, enrich=False)
    assert result["games"] == 2
    assert result["snapshots"] == 2

    from app.models import Game, LibraryEntry, PlaytimeSnapshot

    assert session.query(Game).count() == 2
    assert session.query(LibraryEntry).count() == 2
    assert session.query(PlaytimeSnapshot).count() == 2

    # A second sync without playtime changes must not create new snapshots.
    result = sync_library(session, enrich=False)
    assert result["snapshots"] == 0


@respx.mock
def test_private_profile_raises_clear_error(session):
    respx.get(url__regex=r".*GetOwnedGames.*").mock(
        return_value=httpx.Response(200, json={"response": {}})
    )
    try:
        fetch_owned_games("76561197980782631")
    except ValueError as exc:
        assert "öffentlich" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
