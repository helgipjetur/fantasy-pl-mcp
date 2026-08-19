# src/fpl_mcp/fpl/team_state.py
"""Typed reader for the authenticated my-team endpoint.

Internal helper, not an exposed MCP tool. Every write tool reads squad
state through this module so that selling prices always come from the
authenticated endpoint and never from market prices.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .auth_manager import get_auth_manager
from .cache import cache, get_player_map

logger = logging.getLogger(__name__)

# element_type id -> position code, as used across the codebase
POSITION_CODES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


@dataclass
class Pick:
    """One of the 15 players in the current squad."""

    element: int                 # player id
    position: int                # 1-11 starting XI, 12-15 bench
    is_captain: bool
    is_vice_captain: bool
    purchase_price: int          # tenths of a million, as the API returns it
    selling_price: int           # tenths of a million; NEVER substitute now_cost
    # Enrichment from bootstrap-static
    web_name: str = ""
    element_type: int = 0        # 1 GKP, 2 DEF, 3 MID, 4 FWD
    club_id: int = 0             # Premier League club id

    @property
    def position_code(self) -> str:
        return POSITION_CODES.get(self.element_type, "UNK")

    @property
    def on_bench(self) -> bool:
        return self.position > 11


@dataclass
class TransferState:
    """Transfer allowance and budget, straight from the my-team endpoint."""

    limit: Optional[int]         # free transfers available; None = unlimited
    cost: int                    # points hit per transfer beyond the limit
    made: int                    # transfers already made this gameweek
    bank: int                    # tenths of a million
    value: int                   # squad value in tenths of a million


@dataclass
class ChipState:
    name: str
    status_for_entry: str        # e.g. "available", "played", "unavailable"


@dataclass
class TeamState:
    """Full authenticated squad state used by the write tools."""

    entry_id: int
    picks: List[Pick] = field(default_factory=list)
    transfers: TransferState = None
    chips: List[ChipState] = field(default_factory=list)

    def pick_by_element(self, element: int) -> Optional[Pick]:
        for p in self.picks:
            if p.element == element:
                return p
        return None


async def fetch_team_state(use_cache: bool = False) -> TeamState:
    """Fetch the authenticated user's current squad state.

    Args:
        use_cache: Allow the short-lived my-team cache. Write tools pass
            False so plans and post-write confirmations are never stale.

    Returns:
        TeamState with per-player selling prices from the authenticated
        endpoint

    Raises:
        ValueError: When no team ID is configured or the response is
            missing required fields
    """
    auth_manager = get_auth_manager()
    team_id = auth_manager.team_id
    if not team_id:
        raise ValueError(
            "No team ID found in credentials. Run 'fpl-mcp-config setup'."
        )
    team_id = int(team_id)

    if not use_cache:
        cache.clear(f"my_team_{team_id}")
    data = await auth_manager.get_my_team(team_id)

    raw_picks = data.get("picks")
    if not raw_picks:
        raise ValueError(
            "my-team endpoint returned no picks; cannot determine squad state"
        )

    player_map = await get_player_map()

    picks = []
    for rp in raw_picks:
        element = rp.get("element")
        info = player_map.get(element, {})
        if rp.get("selling_price") is None:
            raise ValueError(
                f"my-team endpoint returned no selling_price for element "
                f"{element}; refusing to guess"
            )
        picks.append(Pick(
            element=element,
            position=rp.get("position", 0),
            is_captain=rp.get("is_captain", False),
            is_vice_captain=rp.get("is_vice_captain", False),
            purchase_price=rp.get("purchase_price", 0),
            selling_price=rp.get("selling_price"),
            web_name=info.get("web_name", f"Player {element}"),
            element_type=info.get("element_type", 0),
            club_id=info.get("team", 0),
        ))

    raw_transfers = data.get("transfers", {})
    transfers = TransferState(
        limit=raw_transfers.get("limit"),
        cost=raw_transfers.get("cost", 4),
        made=raw_transfers.get("made", 0),
        bank=raw_transfers.get("bank", 0),
        value=raw_transfers.get("value", 0),
    )

    chips = [
        ChipState(name=c.get("name", ""), status_for_entry=c.get("status_for_entry", ""))
        for c in data.get("chips", [])
    ]

    return TeamState(
        entry_id=team_id,
        picks=sorted(picks, key=lambda p: p.position),
        transfers=transfers,
        chips=chips,
    )
