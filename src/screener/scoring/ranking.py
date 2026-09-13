"""Values into percentiles: within a peer group when it has enough, else the market.

The floor is counted per metric (ratios spec D12). A sector holding 112
securities can still hold only 8 that produced a given ratio, and a percentile
is worth exactly the values it was computed from.

Falling back ranks against **every** security that produced the metric. v1 gave
securities in a thin sector a shared `market` group but ranked them only among
the others that also fell, so a thin sector "falling back to the market" was
still ranked against itself (plan amendment A3). It was never exercised --
every sector clears the floor on momentum -- and ratios are the first thing
that would have.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from screener.scoring.percentile import deciles, percentiles


@dataclass(frozen=True)
class Placed:
    security_id: int
    code: str
    value: Decimal
    percentile: Decimal
    peer_group_id: int
    peer_count: int
    level: int


@dataclass(frozen=True)
class GroupStat:
    peer_group_id: int
    code: str
    member_count: int
    deciles: list[Decimal]


def rank(
    values: Mapping[int, Mapping[str, Decimal]],
    groups: Mapping[int, tuple[int, int]],
    *,
    market_id: int,
    higher_is_better: Mapping[str, bool],
    min_peers: int,
) -> tuple[list[Placed], list[GroupStat]]:
    """Every value placed, and one decile row per (group, metric) that ranked.

    `groups` maps a security to `(peer_group_id, level)` as `peers.resolve`
    assigned it. A security already in the market group -- unclassified -- ranks
    against the market like one whose bucket fell short.
    """
    by_code: dict[str, list[tuple[int, Decimal]]] = {}
    for security_id, metrics in values.items():
        for code, value in metrics.items():
            by_code.setdefault(code, []).append((security_id, value))

    placed: list[Placed] = []
    stats: list[GroupStat] = []
    for code, members in by_code.items():
        direction = higher_is_better[code]
        buckets: dict[int, list[tuple[int, Decimal]]] = {}
        for security_id, value in members:
            buckets.setdefault(groups[security_id][0], []).append((security_id, value))

        fallen: list[tuple[int, Decimal]] = []
        for group_id, bucket in buckets.items():
            if group_id == market_id or len(bucket) < min_peers:
                fallen.extend(bucket)
                continue
            level = groups[bucket[0][0]][1]
            bucket_values = [value for _, value in bucket]
            ranked = percentiles(bucket_values, higher_is_better=direction)
            for (security_id, value), percentile in zip(bucket, ranked):
                placed.append(
                    Placed(security_id, code, value, percentile, group_id, len(bucket), level)
                )
            stats.append(GroupStat(group_id, code, len(bucket), deciles(bucket_values)))

        if fallen:
            everyone = [value for _, value in members]
            market = dict(
                zip(
                    (security_id for security_id, _ in members),
                    percentiles(everyone, higher_is_better=direction),
                )
            )
            for security_id, value in fallen:
                placed.append(
                    Placed(security_id, code, value, market[security_id], market_id, len(members), 0)
                )
            stats.append(GroupStat(market_id, code, len(members), deciles(everyone)))
    return placed, stats
