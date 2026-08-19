"""Tests for the write tools (make_transfers, set_lineup, set_captain).

All HTTP is mocked; no live calls. Squad fixture: 2 GKP, 5 DEF, 5 MID,
3 FWD with three defenders from club 3, 1 free transfer, £0.5m in the
bank, every squad player selling at £5.0m.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fpl_mcp.fpl.team_state import ChipState, Pick, TeamState, TransferState
from fpl_mcp.fpl.validation import compute_points_hit
from fpl_mcp.fpl.tools.transfers import register_tools


class ToolCollector:
    """Minimal FastMCP stand-in that records registered tool functions."""

    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def collect():
    mcp = ToolCollector()
    register_tools(mcp)
    return mcp.tools


def mk_pick(element, position, etype, club, name, cap=False, vice=False,
            selling=50):
    return Pick(
        element=element, position=position, is_captain=cap,
        is_vice_captain=vice, purchase_price=50, selling_price=selling,
        web_name=name, element_type=etype, club_id=club,
    )


# Squad: XI = GK1, D1-D4, M1-M4, F1-F2; bench = GK2, D5, M5, F3.
# Captain M1, vice F1. D1-D3 all from club 3.
SQUAD_SPEC = [
    # (element, position, element_type, club, name, cap, vice)
    (1, 1, 1, 1, "GK1"),
    (3, 2, 2, 3, "D1"),
    (4, 3, 2, 3, "D2"),
    (5, 4, 2, 3, "D3"),
    (6, 5, 2, 4, "D4"),
    (8, 6, 3, 6, "M1", True, False),
    (9, 7, 3, 7, "M2"),
    (10, 8, 3, 8, "M3"),
    (11, 9, 3, 9, "M4"),
    (13, 10, 4, 11, "F1", False, True),
    (14, 11, 4, 12, "F2"),
    (2, 12, 1, 2, "GK2"),
    (7, 13, 2, 5, "D5"),
    (12, 14, 3, 10, "M5"),
    (15, 15, 4, 13, "F3"),
]


def make_state(limit=1, cost=4, bank=5, chips=()):
    picks = [mk_pick(*spec) for spec in SQUAD_SPEC]
    return TeamState(
        entry_id=999,
        picks=sorted(picks, key=lambda p: p.position),
        transfers=TransferState(limit=limit, cost=cost, made=0, bank=bank,
                                value=1000),
        chips=list(chips),
    )


def pool_entry(pid, name, etype, club, cost, points=100):
    return {
        "id": pid, "web_name": name, "first_name": name,
        "second_name": name, "element_type": etype, "team": club,
        "now_cost": cost, "total_points": points,
    }


# Player pool: the squad itself plus incoming candidates
PLAYER_POOL = {
    p["id"]: p for p in (
        [pool_entry(s[0], s[4], s[2], s[3], 50) for s in SQUAD_SPEC] + [
            pool_entry(101, "CheapFwd", 4, 15, 45),
            pool_entry(102, "PriceyFwd", 4, 16, 145),
            pool_entry(103, "Club3Def", 2, 3, 45),
            pool_entry(104, "MidIn", 3, 17, 45),
            pool_entry(107, "OtherFwd", 4, 18, 45),
            pool_entry(108, "Havertz", 4, 19, 45, points=120),
            pool_entry(109, "Haverson", 4, 20, 45, points=80),
        ]
    )
}

TEAMS = [{"id": i, "name": f"Club {i}"} for i in range(1, 21)]

FUTURE_GWS = [
    {"id": 2, "is_next": True, "deadline_time": "2030-08-01T17:30:00Z"},
]
PAST_GWS = [
    {"id": 2, "is_next": True, "deadline_time": "2020-08-01T17:30:00Z"},
]


def patch_env(state=None, gameweeks=None, post_response=None,
              states=None):
    """Patch state fetching, player pool, clubs, gameweeks, and the POST."""
    if states is None:
        states = [state or make_state()]
    if post_response is None:
        post_response = MagicMock(status_code=200, json=lambda: {})
    return patch.multiple(
        "fpl_mcp.fpl.tools.transfers",
        fetch_team_state=AsyncMock(side_effect=list(states) * 4),
        get_player_map=AsyncMock(return_value=PLAYER_POOL),
    ), patch.multiple(
        "fpl_mcp.fpl.api.FPLAPI",
        get_teams=AsyncMock(return_value=TEAMS),
        get_gameweeks=AsyncMock(return_value=gameweeks or FUTURE_GWS),
    ), patch(
        "fpl_mcp.fpl.auth_manager.FPLAuthManager.make_authed_post",
        new=AsyncMock(return_value=post_response),
    )


async def run_tool(name, gameweeks=None, state=None, states=None,
                   post_response=None, **kwargs):
    tools = collect()
    p1, p2, p3 = patch_env(state=state, gameweeks=gameweeks,
                           states=states, post_response=post_response)
    with p1, p2, p3 as post_mock:
        result = await tools[name](**kwargs)
    return result, post_mock


# ---------------------------------------------------------------- transfers

async def test_valid_transfer_dry_run():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": 101}],
    )
    assert "OUT F1 (sell £5.0m)" in result
    assert "IN CheapFwd (buy £4.5m)" in result
    assert "Bank after: £1.0m" in result
    assert "Points hit: 0" in result
    assert "DRY RUN" in result
    post.assert_not_called()


async def test_transfer_insufficient_funds():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": "PriceyFwd"}],
    )
    assert result.startswith("REJECTED")
    assert "Insufficient funds" in result
    assert "short by £9.0m" in result
    post.assert_not_called()


async def test_transfer_fourth_player_from_club():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "D4", "in": "Club3Def"}],
    )
    assert result.startswith("REJECTED")
    assert "Max 3 players from one club" in result
    assert "Club 3" in result
    post.assert_not_called()


async def test_transfer_breaks_composition():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": "MidIn"}],
    )
    assert result.startswith("REJECTED")
    assert "exactly 5 MID" in result
    post.assert_not_called()


async def test_transfer_in_player_already_owned():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": "M1"}],
    )
    assert result.startswith("REJECTED")
    assert "already in your squad" in result
    post.assert_not_called()


def test_points_hit_computation():
    assert compute_points_hit(1, 1, 4) == 0
    assert compute_points_hit(2, 1, 4) == 4
    assert compute_points_hit(3, 1, 4) == 8
    assert compute_points_hit(2, 2, 4) == 0
    # None allowance = unlimited (pre-season / chip): never a hit
    assert compute_points_hit(9, None, 4) == 0


async def test_two_transfers_report_four_point_hit():
    result, post = await run_tool(
        "make_transfers",
        transfers=[{"out": "F1", "in": 101}, {"out": "F2", "in": 107}],
    )
    assert "Points hit: 4" in result
    assert "DRY RUN" in result
    post.assert_not_called()


async def test_hit_blocked_without_confirm_hit():
    result, post = await run_tool(
        "make_transfers",
        transfers=[{"out": "F1", "in": 101}, {"out": "F2", "in": 107}],
        dry_run=False, confirm_hit=False,
    )
    assert "REFUSED" in result
    assert "confirm_hit" in result
    assert "Nothing was submitted" in result
    post.assert_not_called()


async def test_transfer_execution_posts_once_and_confirms():
    pre = make_state()
    post_state = make_state()
    # After the swap, F1 (13) is replaced by CheapFwd (101)
    post_state.picks = [p for p in post_state.picks if p.element != 13]
    post_state.picks.append(mk_pick(101, 10, 4, 15, "CheapFwd", vice=True,
                                    selling=45))
    post_state.picks.sort(key=lambda p: p.position)

    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": 101}],
        dry_run=False, states=[pre, post_state],
    )
    assert "submitted and confirmed" in result
    post.assert_called_once()
    url, payload = post.call_args.args
    assert url.endswith("/transfers/")
    assert payload["entry"] == 999
    assert payload["event"] == 2
    assert payload["chip"] is None
    assert payload["transfers"] == [{
        "element_in": 101, "element_out": 13,
        "purchase_price": 45, "selling_price": 50,
    }]


async def test_transfer_execution_rejected_by_api():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": 101}],
        dry_run=False,
        post_response=MagicMock(
            status_code=403, json=lambda: {"error": "nope"}, text="nope"),
    )
    assert "rejected — nothing was submitted" in result
    assert "HTTP 403" in result
    assert "NOT retried" in result
    post.assert_called_once()


async def test_ambiguous_incoming_name_lists_candidates():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": "Haver"}],
    )
    assert result.startswith("REJECTED")
    assert "ambiguous" in result
    assert "Havertz" in result and "Haverson" in result
    post.assert_not_called()


async def test_unknown_incoming_name():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": "Zzzz"}],
    )
    assert result.startswith("REJECTED")
    assert "No player matches 'Zzzz'" in result
    post.assert_not_called()


async def test_picks_chip_rejected_on_transfers():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": 101}],
        chip="bboost",
    )
    assert result.startswith("REJECTED")
    assert "cannot be played with this tool" in result
    post.assert_not_called()


async def test_unavailable_chip_rejected():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": 101}],
        chip="wildcard",
    )
    assert result.startswith("REJECTED")
    assert "not available" in result
    post.assert_not_called()


async def test_wildcard_makes_transfers_free():
    state = make_state(
        chips=[ChipState(name="wildcard", status_for_entry="available")]
    )
    result, post = await run_tool(
        "make_transfers",
        transfers=[{"out": "F1", "in": 101}, {"out": "F2", "in": 107}],
        state=state, chip="wildcard",
    )
    assert "CHIP ACTIVE: wildcard" in result
    assert "Points hit: 0" in result
    assert "DRY RUN" in result
    post.assert_not_called()


async def test_triple_captain_via_set_captain():
    state = make_state(
        chips=[ChipState(name="3xc", status_for_entry="available")]
    )
    result, post = await run_tool(
        "set_captain", captain="M2", state=state, chip="3xc",
    )
    assert "CHIP ACTIVE: 3xc" in result
    assert "DRY RUN" in result
    post.assert_not_called()


async def test_chip_payload_carries_chip_name():
    pre = make_state(
        chips=[ChipState(name="bboost", status_for_entry="available")]
    )
    post_state = make_state()
    result, post = await run_tool(
        "set_lineup", starting=XI_NAMES, bench_order=BENCH_NAMES,
        dry_run=False, chip="bboost", states=[pre, post_state],
    )
    post.assert_called_once()
    _, payload = post.call_args.args
    assert payload["chip"] == "bboost"


async def test_deadline_passed_rejects_write():
    result, post = await run_tool(
        "make_transfers", transfers=[{"out": "F1", "in": 101}],
        gameweeks=PAST_GWS,
    )
    assert result.startswith("REJECTED")
    assert "deadline has passed" in result
    assert "2020-08-01" in result
    post.assert_not_called()


async def test_expired_token_gives_reauth_message():
    tools = collect()
    msg = ("FPL refresh token is invalid or expired. Re-run "
           "'fpl-mcp-config setup'.")
    with patch(
        "fpl_mcp.fpl.tools.transfers.fetch_team_state",
        new=AsyncMock(side_effect=ValueError(msg)),
    ):
        result = await tools["make_transfers"](
            transfers=[{"out": "F1", "in": 101}])
    assert "refresh token is invalid or expired" in result
    assert "fpl-mcp-config setup" in result
    assert "Traceback" not in result


# ------------------------------------------------------------------ lineup

XI_NAMES = ["GK1", "D1", "D2", "D3", "D4", "M1", "M2", "M3", "M4", "F1", "F2"]
BENCH_NAMES = ["GK2", "D5", "M5", "F3"]


async def test_lineup_rejects_two_goalkeepers():
    starting = ["GK1", "GK2", "D1", "D2", "D3", "M1", "M2", "M3", "M4",
                "F1", "F2"]
    bench = ["D4", "D5", "M5", "F3"]
    result, post = await run_tool(
        "set_lineup", starting=starting, bench_order=bench,
    )
    assert result.startswith("REJECTED")
    assert "exactly 1 GKP" in result
    post.assert_not_called()


async def test_lineup_rejects_two_defenders():
    starting = ["GK1", "D1", "D2", "M1", "M2", "M3", "M4", "M5", "F1",
                "F2", "F3"]
    bench = ["GK2", "D3", "D4", "D5"]
    result, post = await run_tool(
        "set_lineup", starting=starting, bench_order=bench,
    )
    assert result.startswith("REJECTED")
    assert "at least 3 DEF" in result
    post.assert_not_called()


async def test_lineup_rejects_outfielder_in_bench_slot_one():
    result, post = await run_tool(
        "set_lineup", starting=XI_NAMES,
        bench_order=["D5", "GK2", "M5", "F3"],
    )
    assert result.startswith("REJECTED")
    assert "reserved for the backup GKP" in result
    post.assert_not_called()


async def test_lineup_dry_run_swap_shows_diff():
    starting = ["GK1", "D1", "D2", "D3", "D4", "M1", "M2", "M3", "M4",
                "F1", "F3"]  # F3 in for F2
    bench = ["GK2", "D5", "M5", "F2"]
    result, post = await run_tool(
        "set_lineup", starting=starting, bench_order=bench,
    )
    assert "DRY RUN" in result
    assert "into the XI" in result
    assert "benched" in result
    post.assert_not_called()


async def test_lineup_must_cover_whole_squad():
    result, post = await run_tool(
        "set_lineup", starting=XI_NAMES,
        bench_order=["GK2", "D5", "M5", "M5"],  # F3 missing, M5 twice
    )
    assert result.startswith("REJECTED")
    post.assert_not_called()


# ----------------------------------------------------------------- captain

async def test_set_captain_dry_run_diff():
    result, post = await run_tool("set_captain", captain="M2")
    assert "DRY RUN" in result
    assert "gains (C)" in result
    assert "loses (C)" in result
    post.assert_not_called()


async def test_set_captain_executes_and_confirms():
    pre = make_state()
    post_state = make_state()
    for p in post_state.picks:
        p.is_captain = p.web_name == "M2"
        p.is_vice_captain = p.web_name == "F1"

    result, post = await run_tool(
        "set_captain", captain="M2", dry_run=False,
        states=[pre, post_state],
    )
    assert "submitted and confirmed" in result
    post.assert_called_once()
    url, payload = post.call_args.args
    assert url.endswith("/my-team/999/")
    picks = payload["picks"]
    assert len(picks) == 15
    by_el = {p["element"]: p for p in picks}
    assert by_el[9]["is_captain"] is True          # M2
    assert by_el[8]["is_captain"] is False         # M1
    assert by_el[13]["is_vice_captain"] is True    # F1 unchanged
    # Positions untouched
    assert [p["position"] for p in picks] == list(range(1, 16))


async def test_set_captain_rejects_benched_player():
    result, post = await run_tool("set_captain", captain="GK2")
    assert result.startswith("REJECTED")
    assert "not in the starting XI" in result
    post.assert_not_called()


async def test_set_captain_rejects_same_captain_and_vice():
    result, post = await run_tool(
        "set_captain", captain="M2", vice_captain="M2",
    )
    assert result.startswith("REJECTED")
    assert "must be different players" in result
    post.assert_not_called()


async def test_set_captain_of_current_vice_swaps_armbands():
    # New captain is the current vice; the old captain takes the vice band
    result, post = await run_tool("set_captain", captain="F1")
    assert "DRY RUN" in result
    assert "REJECTED" not in result
    post.assert_not_called()
