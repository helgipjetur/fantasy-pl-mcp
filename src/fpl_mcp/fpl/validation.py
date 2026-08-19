# src/fpl_mcp/fpl/validation.py
"""Pure validation rules for FPL squad writes.

No network access. Every write tool runs the relevant checks here before
any HTTP call. Failures raise ValidationError with a message naming the
specific rule broken and the offending players.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence

# element_type -> (code, required squad count)
SQUAD_COMPOSITION = {1: ("GKP", 2), 2: ("DEF", 5), 3: ("MID", 5), 4: ("FWD", 3)}
MAX_PER_CLUB = 3
SQUAD_SIZE = 15
XI_SIZE = 11

# Starting XI minimums per position (position 1 handled separately: exactly 1 GKP)
XI_MINIMUMS = {2: ("DEF", 3), 3: ("MID", 2), 4: ("FWD", 1)}


class ValidationError(ValueError):
    """A squad rule was broken. The message names the rule and the players."""


@dataclass
class SquadPlayer:
    """Minimal player view the validators need.

    Prices are in tenths of a million, matching the API. selling_price is
    only meaningful for players currently owned; now_cost for incoming ones.
    """

    element: int
    name: str
    element_type: int      # 1 GKP, 2 DEF, 3 MID, 4 FWD
    club_id: int
    club_name: str = ""


def _names(players: Iterable[SquadPlayer]) -> str:
    return ", ".join(p.name for p in players)


def validate_squad_composition(squad: Sequence[SquadPlayer]) -> None:
    """The 15-player squad must be exactly 2 GKP, 5 DEF, 5 MID, 3 FWD."""
    if len(squad) != SQUAD_SIZE:
        raise ValidationError(
            f"Squad must have exactly {SQUAD_SIZE} players, got {len(squad)}"
        )
    for etype, (code, required) in SQUAD_COMPOSITION.items():
        have = [p for p in squad if p.element_type == etype]
        if len(have) != required:
            raise ValidationError(
                f"Squad must have exactly {required} {code}, would have "
                f"{len(have)}: {_names(have) or 'none'}"
            )


def validate_club_limit(squad: Sequence[SquadPlayer]) -> None:
    """No more than 3 players from any single Premier League club."""
    by_club: Dict[int, List[SquadPlayer]] = {}
    for p in squad:
        by_club.setdefault(p.club_id, []).append(p)
    for club_id, players in by_club.items():
        if len(players) > MAX_PER_CLUB:
            club = players[0].club_name or f"club {club_id}"
            raise ValidationError(
                f"Max {MAX_PER_CLUB} players from one club: would have "
                f"{len(players)} from {club}: {_names(players)}"
            )


def validate_budget(
    bank: int,
    outgoing_selling_prices: Sequence[int],
    incoming_costs: Sequence[int],
) -> int:
    """Bank must stay >= 0 after the transaction.

    Args:
        bank: Current bank in tenths of a million
        outgoing_selling_prices: selling_price (NOT now_cost) of each
            outgoing player, tenths of a million
        incoming_costs: now_cost of each incoming player, tenths

    Returns:
        The resulting bank in tenths of a million

    Raises:
        ValidationError: naming the shortfall when funds are insufficient
    """
    resulting = bank + sum(outgoing_selling_prices) - sum(incoming_costs)
    if resulting < 0:
        raise ValidationError(
            f"Insufficient funds: bank after transfers would be "
            f"£{resulting / 10.0:.1f}m (short by £{-resulting / 10.0:.1f}m). "
            f"Bank £{bank / 10.0:.1f}m + sales £{sum(outgoing_selling_prices) / 10.0:.1f}m "
            f"- purchases £{sum(incoming_costs) / 10.0:.1f}m"
        )
    return resulting


def validate_starting_xi(
    starting: Sequence[SquadPlayer],
    bench: Sequence[SquadPlayer],
) -> None:
    """Starting XI and bench must form a legal lineup.

    Rules: exactly 11 starters with exactly 1 GKP, at least 3 DEF, 2 MID
    and 1 FWD; exactly 4 on the bench with the backup GKP in bench slot 1
    (squad position 12).
    """
    if len(starting) != XI_SIZE:
        raise ValidationError(
            f"Starting XI must have exactly {XI_SIZE} players, got {len(starting)}"
        )
    if len(bench) != SQUAD_SIZE - XI_SIZE:
        raise ValidationError(
            f"Bench must have exactly {SQUAD_SIZE - XI_SIZE} players, got {len(bench)}"
        )

    keepers = [p for p in starting if p.element_type == 1]
    if len(keepers) != 1:
        raise ValidationError(
            f"Starting XI must have exactly 1 GKP, would have "
            f"{len(keepers)}: {_names(keepers) or 'none'}"
        )

    for etype, (code, minimum) in XI_MINIMUMS.items():
        have = [p for p in starting if p.element_type == etype]
        if len(have) < minimum:
            raise ValidationError(
                f"Starting XI must have at least {minimum} {code}, would "
                f"have {len(have)}: {_names(have) or 'none'}"
            )

    if bench and bench[0].element_type != 1:
        bench_keepers = [p for p in bench if p.element_type == 1]
        raise ValidationError(
            f"Bench position 12 is reserved for the backup GKP, got "
            f"{bench[0].name}"
            + (f" (backup GKP is {_names(bench_keepers)})" if bench_keepers else "")
        )


def validate_captaincy(
    starting: Sequence[SquadPlayer],
    captain: Optional[SquadPlayer],
    vice_captain: Optional[SquadPlayer],
) -> None:
    """Exactly one captain and one vice, different players, both starters."""
    if captain is None:
        raise ValidationError("A captain must be set")
    if vice_captain is None:
        raise ValidationError("A vice captain must be set")
    if captain.element == vice_captain.element:
        raise ValidationError(
            f"Captain and vice captain must be different players, both are "
            f"{captain.name}"
        )
    starter_ids = {p.element for p in starting}
    if captain.element not in starter_ids:
        raise ValidationError(
            f"Captain {captain.name} is not in the starting XI"
        )
    if vice_captain.element not in starter_ids:
        raise ValidationError(
            f"Vice captain {vice_captain.name} is not in the starting XI"
        )


def validate_deadline(
    deadline_time_utc: datetime,
    now_utc: Optional[datetime] = None,
    gameweek_id: Optional[int] = None,
) -> None:
    """Reject any write after the next gameweek's deadline has passed.

    Args:
        deadline_time_utc: The gameweek deadline as an aware UTC datetime
        now_utc: Injectable clock for tests; defaults to the current UTC time
        gameweek_id: Included in the error message when known
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if deadline_time_utc.tzinfo is None:
        deadline_time_utc = deadline_time_utc.replace(tzinfo=timezone.utc)
    if now_utc >= deadline_time_utc:
        gw = f"gameweek {gameweek_id} " if gameweek_id else ""
        raise ValidationError(
            f"The {gw}deadline has passed "
            f"({deadline_time_utc.isoformat()} UTC); writes are locked until "
            f"the next gameweek opens"
        )


def compute_points_hit(
    n_transfers: int, free_transfers: Optional[int], cost_per_extra: int
) -> int:
    """Points hit for making n_transfers with the given free allowance.

    A None allowance means unlimited (pre-season or an active chip), which
    never incurs a hit.
    """
    if free_transfers is None:
        return 0
    return max(0, n_transfers - free_transfers) * cost_per_extra
