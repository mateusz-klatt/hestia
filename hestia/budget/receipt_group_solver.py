"""Bounded exact-sum proposals for one receipt anchor and transaction."""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from itertools import islice

from .analytics_core import AnalyticsError, _parts

MAX_GROUP_SIZE = 8
MAX_GROUP_POOL = 24
MAX_SOLVER_STEPS = 2_000
MAX_GROUP_PROPOSALS = 5


def find_receipt_groups(
    anchor: tuple[str, str],
    peers: Iterable[tuple[str, str]],
    target_amount: str,
    *,
    same_day_merchant_ids: Collection[str] = (),
    eligible: Callable[[tuple[str, ...]], bool] | None = None,
    max_steps: int = MAX_SOLVER_STEPS,
    max_proposals: int = MAX_GROUP_PROPOSALS,
) -> tuple[list[tuple[str, ...]], int]:
    """Find exact sums of positive decimal amounts, always including the anchor.

    The caller supplies peers in affinity order and adjusts the target for any
    verified fee. Each proposal starts with the anchor, then sorted peer IDs.
    The count includes full-bucket checks, exact pair checks, and DFS visits.
    """
    step_limit = min(max_steps, MAX_SOLVER_STEPS)
    proposal_limit = min(max_proposals, MAX_GROUP_PROPOSALS)
    if step_limit <= 0 or proposal_limit <= 0:
        return [], 0

    target, target_scale = _parts(target_amount)
    if target <= 0:
        raise AnalyticsError()
    anchor_id, anchor_amount = anchor
    parsed = [(anchor_id, _parts(anchor_amount))]
    parsed.extend((receipt_id, _parts(amount)) for receipt_id, amount in islice(peers, MAX_GROUP_POOL))
    scale = max(target_scale, max(places for _, (_, places) in parsed))
    weights: dict[str, int] = {}
    for receipt_id, (coefficient, places) in parsed:
        weight = coefficient * 10 ** (scale - places)
        if weight <= 0 or weights.setdefault(receipt_id, weight) != weight:
            raise AnalyticsError()
    remainder = target * 10 ** (scale - target_scale) - weights.pop(anchor_id)
    if remainder <= 0 or not weights:
        return [], 0

    proposals: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    steps = 0

    def finished() -> bool:
        return steps >= step_limit or len(proposals) >= proposal_limit

    def record(peer_ids: tuple[str, ...]) -> None:
        group = (anchor_id, *sorted(peer_ids))
        if group in seen:
            return
        seen.add(group)
        if eligible is None or eligible(group):
            proposals.append(group)

    full_bucket = tuple(receipt_id for receipt_id in weights if receipt_id in same_day_merchant_ids)
    if 0 < len(full_bucket) < MAX_GROUP_SIZE:
        steps += 1
        if sum(weights[receipt_id] for receipt_id in full_bucket) == remainder:
            record(full_bucket)

    ranked = sorted(weights.items(), key=lambda peer: (-peer[1], peer[0]))
    for receipt_id, weight in ranked:
        if finished():
            break
        if weight == remainder:
            steps += 1
            record((receipt_id,))

    # Larger amounts cannot fit, and equal amounts were handled by the pair pass.
    search = [(receipt_id, weight) for receipt_id, weight in ranked if weight < remainder]
    if finished() or len(search) < 2:
        return proposals, steps
    suffix_totals = [0] * (len(search) + 1)
    for index in range(len(search) - 1, -1, -1):
        suffix_totals[index] = search[index][1] + suffix_totals[index + 1]

    def visit(start: int, needed: int, chosen: tuple[str, ...]) -> None:
        nonlocal steps
        steps += 1
        if needed == 0:
            record(chosen)
            return
        if len(chosen) == MAX_GROUP_SIZE - 1 or suffix_totals[start] < needed:
            return
        for index in range(start, len(search)):
            if finished():
                break
            receipt_id, weight = search[index]
            if weight <= needed:
                visit(index + 1, needed - weight, (*chosen, receipt_id))

    visit(0, remainder, ())
    return proposals, steps
