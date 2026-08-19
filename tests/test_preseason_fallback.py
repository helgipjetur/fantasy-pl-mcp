"""get_my_team must fall back to the my-team endpoint before the first
deadline, when the event picks endpoint still returns 404."""

from unittest.mock import AsyncMock, patch

from fpl_mcp.fpl.tools.team import get_team_for_gameweek

RAW_PLAYERS = [
    {"id": 1, "web_name": "Salah", "first_name": "Mohamed",
     "second_name": "Salah", "team": 14, "element_type": 3, "now_cost": 130},
    {"id": 2, "web_name": "Haaland", "first_name": "Erling",
     "second_name": "Haaland", "team": 13, "element_type": 4, "now_cost": 150},
]
TEAMS = [
    {"id": 13, "name": "Man City", "short_name": "MCI"},
    {"id": 14, "name": "Liverpool", "short_name": "LIV"},
]

MY_TEAM = {
    "picks": [
        {"element": 1, "position": 1, "is_captain": True,
         "is_vice_captain": False, "purchase_price": 130, "selling_price": 130},
        {"element": 2, "position": 12, "is_captain": False,
         "is_vice_captain": True, "purchase_price": 150, "selling_price": 150},
    ],
    "transfers": {"limit": None, "cost": 4, "made": 0, "bank": 0, "value": 1000},
    "chips": [],
}


def patch_env(picks_error):
    return patch.multiple(
        "fpl_mcp.fpl.api.FPLAPI",
        get_players=AsyncMock(return_value=RAW_PLAYERS),
        get_teams=AsyncMock(return_value=TEAMS),
        get_current_gameweek=AsyncMock(return_value={"id": 1}),
    ), patch.multiple(
        "fpl_mcp.fpl.auth_manager.FPLAuthManager",
        get_team_for_gameweek=AsyncMock(side_effect=picks_error),
        get_my_team=AsyncMock(return_value=MY_TEAM),
        get_entry_data=AsyncMock(return_value={"name": "Test Team"}),
    )


async def test_preseason_404_falls_back_to_my_team():
    p1, p2 = patch_env(Exception("404 Client Error: Not Found"))
    with p1, p2, patch(
        "fpl_mcp.fpl.auth_manager.FPLAuthManager.team_id", "999"
    ):
        result = await get_team_for_gameweek(gameweek=1, team_id=999)

    assert "error" not in result
    assert "my-team endpoint" in result["note"]
    assert [p["id"] for p in result["active"]] == [1]
    assert [p["id"] for p in result["bench"]] == [2]
    assert result["captain"]["id"] == 1


async def test_preseason_404_other_team_returns_clear_error():
    p1, p2 = patch_env(Exception("404 Client Error: Not Found"))
    with p1, p2, patch(
        "fpl_mcp.fpl.auth_manager.FPLAuthManager.team_id", "999"
    ):
        result = await get_team_for_gameweek(gameweek=1, team_id=12345)

    assert "error" in result
    assert "deadline" in result["suggestion"]


async def test_non_404_error_is_not_swallowed():
    p1, p2 = patch_env(Exception("500 Server Error"))
    with p1, p2, patch(
        "fpl_mcp.fpl.auth_manager.FPLAuthManager.team_id", "999"
    ):
        result = await get_team_for_gameweek(gameweek=1, team_id=999)

    assert "error" in result
    assert "500" in result["error"]
