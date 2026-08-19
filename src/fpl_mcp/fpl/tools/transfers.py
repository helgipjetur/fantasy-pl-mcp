# src/fpl_mcp/fpl/tools/transfers.py
"""Write tools: make_transfers, set_lineup, set_captain.

All tools default to dry_run=True and never retry a failed write.

TODO(step-0): The write payload shapes and endpoints below follow the
expected contract in the handoff. They MUST be reconciled against real
captured browser requests before the first live write. If the capture
disagrees, the capture wins.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..api import api
from ..auth_manager import get_auth_manager
from ..cache import cache, get_player_map
from ..team_state import Pick, TeamState, fetch_team_state
from ..utils.gameweek import get_next_gameweek_id
from ..validation import (
    SquadPlayer,
    ValidationError,
    compute_points_hit,
    validate_budget,
    validate_captaincy,
    validate_club_limit,
    validate_deadline,
    validate_squad_composition,
    validate_starting_xi,
)
from ...config import FPL_API_BASE_URL

logger = logging.getLogger(__name__)

PlayerRef = Union[str, int]

CHIP_ERROR = "Chip activation is not supported — activate chips in the browser."


def _fmt_money(tenths: int) -> str:
    return f"£{tenths / 10.0:.1f}m"


def _reject_chip(chip: Any) -> None:
    if chip is not None:
        raise ValidationError(CHIP_ERROR)


async def _club_names() -> Dict[int, str]:
    teams = await api.get_teams()
    return {t["id"]: t.get("name", f"club {t['id']}") for t in teams}


def _pick_to_squad_player(pick: Pick, clubs: Dict[int, str]) -> SquadPlayer:
    return SquadPlayer(
        element=pick.element,
        name=pick.web_name,
        element_type=pick.element_type,
        club_id=pick.club_id,
        club_name=clubs.get(pick.club_id, ""),
    )


def _element_to_squad_player(info: Dict[str, Any], clubs: Dict[int, str]) -> SquadPlayer:
    return SquadPlayer(
        element=info["id"],
        name=info.get("web_name", f"Player {info['id']}"),
        element_type=info.get("element_type", 0),
        club_id=info.get("team", 0),
        club_name=clubs.get(info.get("team", 0), ""),
    )


def _resolve_in_squad(ref: PlayerRef, state: TeamState) -> Pick:
    """Resolve a player reference against the current 15-man squad.

    Accepts an element id (int or digit string) or a name. Ambiguous or
    unmatched names raise with the candidates listed — never guess.
    """
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        element = int(ref)
        pick = state.pick_by_element(element)
        if pick is None:
            squad = ", ".join(f"{p.web_name} ({p.element})" for p in state.picks)
            raise ValidationError(
                f"Player id {element} is not in your squad. Squad: {squad}"
            )
        return pick

    term = str(ref).lower().strip()
    matches = [p for p in state.picks if p.web_name.lower() == term]
    if not matches:
        matches = [p for p in state.picks if term in p.web_name.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        squad = ", ".join(p.web_name for p in state.picks)
        raise ValidationError(
            f"'{ref}' does not match anyone in your squad. Squad: {squad}"
        )
    cands = ", ".join(f"{p.web_name} ({p.element})" for p in matches)
    raise ValidationError(
        f"'{ref}' is ambiguous in your squad; candidates: {cands}. "
        f"Use the element id instead."
    )


async def _resolve_any_player(ref: PlayerRef) -> Dict[str, Any]:
    """Resolve a player reference against the full player pool.

    Returns the raw bootstrap element. Ambiguous or unmatched names raise
    with the candidates listed — never guess.
    """
    player_map = await get_player_map()

    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        element = int(ref)
        info = player_map.get(element)
        if info is None:
            raise ValidationError(f"No player exists with id {element}")
        return info

    term = str(ref).lower().strip()
    exact = [
        p for p in player_map.values()
        if p.get("web_name", "").lower() == term
        or f"{p.get('first_name', '')} {p.get('second_name', '')}".lower().strip() == term
    ]
    if len(exact) == 1:
        return exact[0]

    candidates = exact
    if not candidates:
        candidates = [
            p for p in player_map.values()
            if term in p.get("web_name", "").lower()
            or term in f"{p.get('first_name', '')} {p.get('second_name', '')}".lower()
        ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValidationError(f"No player matches '{ref}'")

    clubs = await _club_names()
    listed = ", ".join(
        f"{p.get('web_name')} ({p['id']}, {clubs.get(p.get('team'), '?')})"
        for p in sorted(candidates, key=lambda p: -(p.get("total_points") or 0))[:8]
    )
    raise ValidationError(
        f"'{ref}' is ambiguous; candidates: {listed}. Use the element id "
        f"or a more specific name."
    )


async def _check_deadline() -> Tuple[int, datetime]:
    """Fetch the next gameweek deadline and reject writes past it.

    Returns:
        (gameweek id, deadline as aware UTC datetime)
    """
    gameweeks = await api.get_gameweeks()
    next_id = await get_next_gameweek_id()
    if next_id is None:
        raise ValidationError(
            "Could not determine the next gameweek; refusing to write"
        )
    gw = next((g for g in gameweeks if g.get("id") == next_id), None)
    if gw is None or not gw.get("deadline_time"):
        raise ValidationError(
            f"No deadline found for gameweek {next_id}; refusing to write"
        )
    deadline = datetime.fromisoformat(gw["deadline_time"].replace("Z", "+00:00"))
    validate_deadline(deadline, gameweek_id=next_id)
    return next_id, deadline


def _describe_response(response) -> str:
    try:
        body = json.dumps(response.json(), indent=2)
    except Exception:
        body = (response.text or "")[:500]
    return f"HTTP {response.status_code}\n{body}"


def _squad_summary(state: TeamState) -> str:
    lines = []
    for p in state.picks:
        flags = ""
        if p.is_captain:
            flags = " (C)"
        elif p.is_vice_captain:
            flags = " (VC)"
        bench = " [bench]" if p.on_bench else ""
        lines.append(
            f"  {p.position:>2}. {p.web_name}{flags}{bench} — "
            f"sell {_fmt_money(p.selling_price)}"
        )
    lines.append(f"  Bank: {_fmt_money(state.transfers.bank)}")
    return "\n".join(lines)


async def _execute_transfers_post(
    state: TeamState,
    event_id: int,
    resolved: Sequence[Tuple[Pick, Dict[str, Any]]],
) -> str:
    """POST the transfers payload once and confirm from re-fetched state."""
    auth_manager = get_auth_manager()

    # TODO(step-0): payload shape to be confirmed against captured requests
    payload = {
        "chip": None,
        "entry": state.entry_id,
        "event": event_id,
        "transfers": [
            {
                "element_in": info["id"],
                "element_out": pick.element,
                "purchase_price": info.get("now_cost", 0),
                "selling_price": pick.selling_price,
            }
            for pick, info in resolved
        ],
    }

    url = f"{FPL_API_BASE_URL}/transfers/"
    response = await auth_manager.make_authed_post(url, payload)

    if not (200 <= response.status_code < 300):
        return (
            f"Transfer request was rejected — nothing was submitted.\n"
            f"{_describe_response(response)}\n"
            f"The request was NOT retried. Verify in the browser before "
            f"trying again."
        )

    # Never report success from the HTTP status alone: confirm from state
    new_state = await fetch_team_state(use_cache=False)
    new_ids = {p.element for p in new_state.picks}
    missing = [info.get("web_name") for _, info in resolved if info["id"] not in new_ids]
    still_there = [pick.web_name for pick, _ in resolved if pick.element in new_ids]

    lines = ["Transfers submitted and confirmed." if not (missing or still_there)
             else "Transfer POST returned success but the squad does not match the plan:"]
    if missing:
        lines.append(f"  Expected in squad but missing: {', '.join(missing)}")
    if still_there:
        lines.append(f"  Expected out of squad but still present: {', '.join(still_there)}")
    lines.append("\nPost-transfer squad:")
    lines.append(_squad_summary(new_state))
    return "\n".join(lines)


async def _make_transfers(
    transfers: List[Dict[str, PlayerRef]],
    dry_run: bool = True,
    confirm_hit: bool = False,
    chip: Optional[str] = None,
) -> str:
    _reject_chip(chip)

    if not transfers:
        raise ValidationError("No transfers given")

    state = await fetch_team_state(use_cache=False)
    event_id, deadline = await _check_deadline()
    clubs = await _club_names()

    # Resolve every leg before touching anything
    resolved: List[Tuple[Pick, Dict[str, Any]]] = []
    for t in transfers:
        if "out" not in t or "in" not in t:
            raise ValidationError(
                f"Each transfer needs 'out' and 'in' keys, got: {t}"
            )
        out_pick = _resolve_in_squad(t["out"], state)
        in_info = await _resolve_any_player(t["in"])
        if state.pick_by_element(in_info["id"]) is not None:
            raise ValidationError(
                f"{in_info.get('web_name')} is already in your squad"
            )
        resolved.append((out_pick, in_info))

    out_ids = [p.element for p, _ in resolved]
    in_ids = [i["id"] for _, i in resolved]
    if len(set(out_ids)) != len(out_ids):
        raise ValidationError("The same player appears twice as outgoing")
    if len(set(in_ids)) != len(in_ids):
        raise ValidationError("The same player appears twice as incoming")

    # Build the post-transfer squad and validate it
    new_squad = [
        _pick_to_squad_player(p, clubs)
        for p in state.picks if p.element not in set(out_ids)
    ] + [_element_to_squad_player(i, clubs) for _, i in resolved]

    validate_squad_composition(new_squad)
    validate_club_limit(new_squad)
    resulting_bank = validate_budget(
        state.transfers.bank,
        [p.selling_price for p, _ in resolved],
        [i.get("now_cost", 0) for _, i in resolved],
    )

    points_hit = compute_points_hit(
        len(resolved), state.transfers.limit, state.transfers.cost
    )
    free = state.transfers.limit
    free_text = "unlimited" if free is None else str(free)

    plan = [f"Transfer plan for gameweek {event_id} "
            f"(deadline {deadline.isoformat()} UTC):"]
    for pick, info in resolved:
        plan.append(
            f"  OUT {pick.web_name} (sell {_fmt_money(pick.selling_price)}) "
            f"→ IN {info.get('web_name')} (buy {_fmt_money(info.get('now_cost', 0))})"
        )
    plan.append(f"  Bank after: {_fmt_money(resulting_bank)}")
    plan.append(f"  Free transfers available: {free_text}, used: {len(resolved)}")
    plan.append(f"  Points hit: {points_hit}")
    plan_text = "\n".join(plan)

    if dry_run:
        return f"{plan_text}\n\nDRY RUN — nothing was submitted."

    if points_hit > 0 and not confirm_hit:
        return (
            f"{plan_text}\n\n"
            f"REFUSED: this plan costs a {points_hit}-point hit and "
            f"confirm_hit is not set. Nothing was submitted. Call again "
            f"with confirm_hit=True to accept the hit."
        )

    result = await _execute_transfers_post(state, event_id, resolved)
    return f"{plan_text}\n\n{result}"


def _build_picks_payload(picks: Sequence[Pick]) -> List[Dict[str, Any]]:
    return [
        {
            "element": p.element,
            "position": p.position,
            "is_captain": p.is_captain,
            "is_vice_captain": p.is_vice_captain,
        }
        for p in sorted(picks, key=lambda p: p.position)
    ]


async def _submit_picks(state: TeamState, new_picks: Sequence[Pick]) -> str:
    """POST the full picks array once and confirm from re-fetched state."""
    auth_manager = get_auth_manager()

    # TODO(step-0): payload shape to be confirmed against captured requests
    payload = {"chip": None, "picks": _build_picks_payload(new_picks)}
    url = f"{FPL_API_BASE_URL}/my-team/{state.entry_id}/"
    response = await auth_manager.make_authed_post(url, payload)

    if not (200 <= response.status_code < 300):
        return (
            f"Lineup request was rejected — nothing was submitted.\n"
            f"{_describe_response(response)}\n"
            f"The request was NOT retried. Verify in the browser before "
            f"trying again."
        )

    new_state = await fetch_team_state(use_cache=False)
    wanted = {
        p.element: (p.position, p.is_captain, p.is_vice_captain) for p in new_picks
    }
    got = {
        p.element: (p.position, p.is_captain, p.is_vice_captain)
        for p in new_state.picks
    }
    if wanted == got:
        return "Lineup submitted and confirmed.\n\n" + _squad_summary(new_state)
    return (
        "Lineup POST returned success but the re-fetched team does not "
        "match what was submitted. Verify in the browser.\n\n"
        + _squad_summary(new_state)
    )


def _lineup_diff(before: Sequence[Pick], after: Sequence[Pick]) -> str:
    b = {p.element: p for p in before}
    lines = []
    for p in sorted(after, key=lambda p: p.position):
        old = b.get(p.element)
        changes = []
        if old is not None:
            if old.on_bench != p.on_bench:
                changes.append("benched" if p.on_bench else "into the XI")
            elif old.position != p.position:
                changes.append(f"slot {old.position}→{p.position}")
            if old.is_captain != p.is_captain:
                changes.append("gains (C)" if p.is_captain else "loses (C)")
            if old.is_vice_captain != p.is_vice_captain:
                changes.append("gains (VC)" if p.is_vice_captain else "loses (VC)")
        flags = " (C)" if p.is_captain else (" (VC)" if p.is_vice_captain else "")
        bench = " [bench]" if p.on_bench else ""
        marker = f"   << {', '.join(changes)}" if changes else ""
        lines.append(f"  {p.position:>2}. {p.web_name}{flags}{bench}{marker}")
    return "\n".join(lines)


def _clone_pick(p: Pick, **overrides) -> Pick:
    kwargs = dict(
        element=p.element,
        position=p.position,
        is_captain=p.is_captain,
        is_vice_captain=p.is_vice_captain,
        purchase_price=p.purchase_price,
        selling_price=p.selling_price,
        web_name=p.web_name,
        element_type=p.element_type,
        club_id=p.club_id,
    )
    kwargs.update(overrides)
    return Pick(**kwargs)


async def _set_lineup(
    starting: List[PlayerRef],
    bench_order: List[PlayerRef],
    captain: Optional[PlayerRef] = None,
    vice_captain: Optional[PlayerRef] = None,
    dry_run: bool = True,
    chip: Optional[str] = None,
) -> str:
    _reject_chip(chip)

    state = await fetch_team_state(use_cache=False)
    await _check_deadline()
    clubs = await _club_names()

    starters = [_resolve_in_squad(r, state) for r in starting]
    bench = [_resolve_in_squad(r, state) for r in bench_order]

    chosen = [p.element for p in starters] + [p.element for p in bench]
    if len(set(chosen)) != len(chosen):
        raise ValidationError("A player appears more than once in the lineup")
    squad_ids = {p.element for p in state.picks}
    if set(chosen) != squad_ids:
        missing = [
            p.web_name for p in state.picks if p.element not in set(chosen)
        ]
        raise ValidationError(
            f"Lineup must use all 15 squad players exactly once; missing: "
            f"{', '.join(missing) or 'none'} "
            f"(gave {len(starters)} starters + {len(bench)} bench)"
        )

    cap_pick = _resolve_in_squad(captain, state) if captain is not None else \
        next((p for p in state.picks if p.is_captain), None)
    vice_pick = _resolve_in_squad(vice_captain, state) if vice_captain is not None else \
        next((p for p in state.picks if p.is_vice_captain), None)

    # Canonical XI order: GKP, DEF, MID, FWD (stable within a position)
    starters = sorted(starters, key=lambda p: p.element_type)

    new_picks = []
    for idx, p in enumerate(starters, start=1):
        new_picks.append(_clone_pick(
            p, position=idx,
            is_captain=cap_pick is not None and p.element == cap_pick.element,
            is_vice_captain=vice_pick is not None and p.element == vice_pick.element,
        ))
    for idx, p in enumerate(bench, start=12):
        new_picks.append(_clone_pick(
            p, position=idx, is_captain=False, is_vice_captain=False,
        ))

    sp_start = [_pick_to_squad_player(p, clubs) for p in new_picks[:11]]
    sp_bench = [_pick_to_squad_player(p, clubs) for p in new_picks[11:]]
    validate_starting_xi(sp_start, sp_bench)
    validate_captaincy(
        sp_start,
        next((s for s in sp_start + sp_bench
              if cap_pick and s.element == cap_pick.element), None),
        next((s for s in sp_start + sp_bench
              if vice_pick and s.element == vice_pick.element), None),
    )

    diff = _lineup_diff(state.picks, new_picks)
    if dry_run:
        return f"Planned lineup:\n{diff}\n\nDRY RUN — nothing was submitted."

    result = await _submit_picks(state, new_picks)
    return f"Planned lineup:\n{diff}\n\n{result}"


async def _set_captain(
    captain: PlayerRef,
    vice_captain: Optional[PlayerRef] = None,
    dry_run: bool = True,
) -> str:
    state = await fetch_team_state(use_cache=False)
    await _check_deadline()
    clubs = await _club_names()

    cap_pick = _resolve_in_squad(captain, state)

    if vice_captain is not None:
        # Explicit vice: taken as given, so captain == vice is rejected
        # by validate_captaincy below rather than silently repaired
        vice_pick = _resolve_in_squad(vice_captain, state)
    else:
        # Vice defaults to the current holder; if the new captain IS the
        # current vice, hand the freed armband to the previous captain
        vice_pick = next((p for p in state.picks if p.is_vice_captain), None)
        if vice_pick is not None and cap_pick.element == vice_pick.element:
            prev_cap = next((p for p in state.picks if p.is_captain), None)
            vice_pick = prev_cap if (
                prev_cap is not None and prev_cap.element != cap_pick.element
            ) else None

    # Positions untouched; only the armband flags change
    new_picks = [
        _clone_pick(
            p,
            is_captain=p.element == cap_pick.element,
            is_vice_captain=vice_pick is not None and p.element == vice_pick.element,
        )
        for p in state.picks
    ]

    sp_start = [_pick_to_squad_player(p, clubs) for p in new_picks if not p.on_bench]
    validate_captaincy(
        sp_start,
        _pick_to_squad_player(cap_pick, clubs),
        _pick_to_squad_player(vice_pick, clubs) if vice_pick else None,
    )

    diff = _lineup_diff(state.picks, new_picks)
    if dry_run:
        return f"Planned captaincy change:\n{diff}\n\nDRY RUN — nothing was submitted."

    result = await _submit_picks(state, new_picks)
    return f"Planned captaincy change:\n{diff}\n\n{result}"


def register_tools(mcp):
    @mcp.tool()
    async def make_transfers(
        transfers: List[Dict[str, PlayerRef]],
        dry_run: bool = True,
        confirm_hit: bool = False,
        chip: Optional[str] = None,
    ) -> str:
        """Plan or execute FPL transfers for the next gameweek.

        Args:
            transfers: List of swaps, each {"out": player, "in": player}.
                Players are element ids or names; ambiguous names fail with
                candidates listed.
            dry_run: When True (default), only return the plan — no write.
            confirm_hit: Must be True to execute a plan that costs a points
                hit; without it the tool refuses even when dry_run=False.
            chip: Not supported; any value is rejected. Activate chips in
                the browser.

        Returns:
            The transfer plan, and on execution the confirmed
            post-transfer squad
        """
        try:
            return await _make_transfers(
                transfers, dry_run=dry_run, confirm_hit=confirm_hit, chip=chip
            )
        except (ValidationError, ValueError) as e:
            return f"REJECTED: {e}"
        except Exception as e:
            logger.error(f"Error in make_transfers: {e}")
            return f"ERROR: {e}"

    @mcp.tool()
    async def set_lineup(
        starting: List[PlayerRef],
        bench_order: List[PlayerRef],
        captain: Optional[PlayerRef] = None,
        vice_captain: Optional[PlayerRef] = None,
        dry_run: bool = True,
        chip: Optional[str] = None,
    ) -> str:
        """Set your starting XI, bench order, and optionally the armbands.

        Args:
            starting: The 11 starters (element ids or names)
            bench_order: The 4 bench players in substitution order; the
                backup goalkeeper must be first
            captain: New captain; defaults to keeping the current one
            vice_captain: New vice captain; defaults to the current one
            dry_run: When True (default), only return the before/after
                diff — no write
            chip: Not supported; any value is rejected. Activate chips in
                the browser.

        Returns:
            A readable before/after diff, and on execution the confirmed
            lineup
        """
        try:
            return await _set_lineup(
                starting, bench_order,
                captain=captain, vice_captain=vice_captain, dry_run=dry_run,
                chip=chip,
            )
        except (ValidationError, ValueError) as e:
            return f"REJECTED: {e}"
        except Exception as e:
            logger.error(f"Error in set_lineup: {e}")
            return f"ERROR: {e}"

    @mcp.tool()
    async def set_captain(
        captain: PlayerRef,
        vice_captain: Optional[PlayerRef] = None,
        dry_run: bool = True,
    ) -> str:
        """Change your captain (and optionally vice captain) only.

        Positions are left untouched; only the armband flags change.

        Args:
            captain: New captain (element id or name); must be a starter
            vice_captain: New vice captain; defaults to the current one
            dry_run: When True (default), only return the before/after
                diff — no write

        Returns:
            A readable before/after diff, and on execution the confirmed
            armbands
        """
        try:
            return await _set_captain(
                captain, vice_captain=vice_captain, dry_run=dry_run
            )
        except (ValidationError, ValueError) as e:
            return f"REJECTED: {e}"
        except Exception as e:
            logger.error(f"Error in set_captain: {e}")
            return f"ERROR: {e}"
