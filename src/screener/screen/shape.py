"""Typed rows into the JSON the screen endpoints send (ui-swap spec D5, D9, D10).

Pure. Every figure leaves as a decimal string, never a float: a stored value and
its reproduction that differ in the seventh digit must not arrive as two floats
that print identically (D5). Rounding happens here, once, half up, so the page
formats units and never rounds again.
"""

from collections.abc import Collection, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from screener.scoring import CODES, RATIO_CODES, Action, adjusted_closes
from screener.screen.explain import Check, Reproduction
from screener.screen.rows import (
    ActionRow,
    BarRow,
    ClassificationRow,
    MetricInfoRow,
    MetricRow,
    PillarRow,
    PreviousRunRow,
    RunRow,
    ScreenRow,
    SectorRow,
    SnapshotRow,
    SymbolRow,
    TilesRow,
)

PILLAR_ORDER: tuple[str, ...] = ("valuation", "quality", "momentum")
PILLAR_KEYS: dict[str, str] = {"valuation": "V", "quality": "Q", "momentum": "M"}
ALL_CODES: tuple[str, ...] = (*RATIO_CODES, *CODES)

PERCENT = "percent"
MULTIPLE = "multiple"
# D5: three metrics are multiples; every other is a fraction shown as a percent.
UNITS: dict[str, str] = {
    code: MULTIPLE if code in ("book_yield", "debt_to_equity", "interest_cover") else PERCENT
    for code in ALL_CODES
}

_TENTH = Decimal("0.1")
_CENT = Decimal("0.01")
_WHOLE = Decimal(1)
# D5: a close of a thousand or more is shown without decimals.
_WHOLE_PRICE_FROM = Decimal(1000)


def one_decimal(value: Decimal | None) -> str | None:
    """A score or a percentile."""
    return None if value is None else str(value.quantize(_TENTH, rounding=ROUND_HALF_UP))


def exact(value: Decimal | None) -> str | None:
    """A raw or reproduced metric value, or a coverage, unrounded.

    Fixed-point rather than `str`, which writes a stored `1E+1` in exponent form
    that the page would then have to parse.
    """
    return None if value is None else format(value, "f")


def price(value: Decimal) -> str:
    step = _WHOLE if abs(value) >= _WHOLE_PRICE_FROM else _CENT
    return str(value.quantize(step, rounding=ROUND_HALF_UP))


def delta(row: ScreenRow) -> Decimal | None:
    """The change since the previous comparable night, or None when there is nothing to compare."""
    return None if row.previous_score is None else row.score - row.previous_score


def partial(min_coverage: Decimal) -> bool:
    return min_coverage < 1


def closes(
    bars: Sequence[BarRow], actions: Sequence[ActionRow], count: int
) -> list[list[str]]:
    """The newest `count` adjusted closes, as `[date, price]` pairs (D11).

    Adjusted by scoring's own `adjusted_closes`, so the chart and momentum cannot
    disagree about a split -- including one Yahoo had already applied to a bar by
    the time it was fetched.
    """
    adjusted = adjusted_closes(
        [(bar.trade_date, bar.close, bar.observed_at) for bar in bars],
        [Action(a.effective_date, a.action_type, a.ratio, a.amount) for a in actions],
    )
    return [[day.isoformat(), price(close)] for day, close in adjusted[-count:]]


def awaiting() -> dict[str, Any]:
    """No v2 night qualifies yet (D7). No time is promised: this process does not hold the scheduler's."""
    return {"state": "awaiting_first_night"}


def sector(code: str, name: str) -> dict[str, str]:
    return {"code": code, "name": name}


def run_payload(run: RunRow) -> dict[str, Any]:
    return {
        "id": run.id,
        "as_of": run.as_of.isoformat(),
        "started_at": run.started_at.isoformat(),
        "finished_at": None if run.finished_at is None else run.finished_at.isoformat(),
        "git_sha": run.git_sha,
        "config_hash": run.config_hash,
        "weight_version": run.weight_version,
        "cutoff_offset_seconds": run.cutoff_offset_seconds,
        "logic": run.logic,
        "emits_alerts": run.emits_alerts,
    }


def _pillar(score: Decimal | None, coverage: Decimal | None) -> dict[str, str | None] | None:
    if score is None:
        return None
    return {"score": one_decimal(score), "coverage": exact(coverage)}


def screen_row(row: ScreenRow, row_closes: list[list[str]]) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "name": row.name,
        "sector": sector(row.sector_code, row.sector_name),
        "score": one_decimal(row.score),
        "delta": one_decimal(delta(row)),
        "pillars": {
            "V": _pillar(row.v_score, row.v_coverage),
            "Q": _pillar(row.q_score, row.q_coverage),
            "M": _pillar(row.m_score, row.m_coverage),
        },
        "agreement": row.pillar_agreement,
        "min_coverage": exact(row.min_coverage),
        "partial": partial(row.min_coverage),
        "closes": row_closes,
    }


def screen_payload(
    *,
    run: RunRow,
    latest: bool,
    previous: PreviousRunRow | None,
    tiles: TilesRow,
    sectors: Sequence[SectorRow],
    total: int,
    rows: Sequence[ScreenRow],
    closes_by_security: Mapping[int, list[list[str]]],
) -> dict[str, Any]:
    return {
        "state": "ready",
        "latest": latest,
        "run": run_payload(run),
        "previous_as_of": None if previous is None else previous.as_of.isoformat(),
        "tiles": {
            "scored": tiles.scored,
            "active_now": tiles.active_now,
            "partial": tiles.partial,
            "agreement_3": tiles.agreement_3,
            "market_ranked_values": tiles.market_ranked_values,
        },
        "sectors": [sector(row.code, row.name) for row in sectors],
        "total": total,
        "rows": [
            screen_row(row, closes_by_security.get(row.security_id, [])) for row in rows
        ],
    }


def listed(
    expected: Mapping[str, Sequence[str]],
    stored: Collection[str],
    pillar_of: Mapping[str, str],
) -> dict[str, list[str]]:
    """Each pillar's metrics to show: every one that applies, plus any the run
    stored that no longer does, so a reclassification cannot hide a stored value
    (plan amendment P4)."""
    return {
        pillar: [
            code
            for code in ALL_CODES
            if pillar_of[code] == pillar and (code in expected[pillar] or code in stored)
        ]
        for pillar in PILLAR_ORDER
    }


def unscored_payload(
    *,
    run: RunRow,
    latest: bool,
    match: SymbolRow,
    where: ClassificationRow,
    closes: list[list[str]],
) -> dict[str, Any]:
    return {
        "scored": False,
        "run_id": run.id,
        "latest": latest,
        "symbol": match.symbol,
        "name": match.name,
        "sector": sector(where.sector_code, where.sector_name),
        "active": match.is_active,
        "closes": closes,
    }


def _stored(row: MetricRow) -> dict[str, Any]:
    return {
        "raw": exact(row.raw_value),
        "percentile": one_decimal(row.percentile),
        "peer_group": row.peer_group,
        "peer_count": row.peer_count,
        "market_ranked": row.fallback_level == 0,
        "period_basis": row.period_basis,
        "period_end": None if row.period_end is None else row.period_end.isoformat(),
    }


def security_payload(
    *,
    run: RunRow,
    latest: bool,
    match: SymbolRow,
    where: ClassificationRow,
    snapshot: SnapshotRow,
    pillars: Sequence[PillarRow],
    metrics: Sequence[MetricRow],
    info: Mapping[str, MetricInfoRow],
    expected: Mapping[str, Sequence[str]],
    checks: Mapping[str, Check],
    reproduction: Reproduction,
    running_build: str,
    closes: list[list[str]],
) -> dict[str, Any]:
    stored = {row.code: row for row in metrics}
    scores = {row.pillar_code: row for row in pillars}
    shown = listed(expected, stored, {code: row.pillar_code for code, row in info.items()})
    industry = (
        None
        if where.industry_code is None or where.industry_name is None
        else {"code": where.industry_code, "name": where.industry_name}
    )
    return {
        "scored": True,
        "run_id": run.id,
        "latest": latest,
        "symbol": match.symbol,
        "name": match.name,
        "sector": sector(where.sector_code, where.sector_name),
        "industry": industry,
        "active": match.is_active,
        "score": one_decimal(snapshot.score),
        "agreement": snapshot.pillar_agreement,
        "partial": partial(snapshot.min_coverage),
        "pillars": [
            {
                "code": pillar,
                "key": PILLAR_KEYS[pillar],
                "score": one_decimal(scores[pillar].score) if pillar in scores else None,
                # A stored code the run's industry no longer applies stays listed
                # as its own row (a mismatch/absent check), but does not count
                # towards `present`, which is a count against `expected` alone.
                "present": sum(
                    1 for code in shown[pillar] if code in stored and code in expected[pillar]
                ),
                "expected": len(expected[pillar]),
                "metrics": [
                    {
                        "code": code,
                        "name": info[code].name,
                        "unit": UNITS[code],
                        "higher_is_better": info[code].higher_is_better,
                        "status": checks[code].status,
                        "stored": _stored(stored[code]) if code in stored else None,
                        "reproduced": exact(checks[code].reproduced),
                        "reason": checks[code].reason,
                    }
                    for code in shown[pillar]
                ],
            }
            for pillar in PILLAR_ORDER
        ],
        "reproduction": {
            "visible_through": reproduction.visible_through.isoformat(),
            "run_build": run.git_sha,
            "running_build": running_build,
            "refreshed_inputs": sorted(reproduction.refreshed),
        },
        "closes": closes,
    }
