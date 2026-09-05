"""Recording what happened.

Writing an audit row must never be the reason an operation fails. Every entry
point here swallows its own errors and logs them, because losing a log line is
a smaller problem than a bot that stops answering when Postgres blinks.
"""

import logging
from decimal import Decimal
from typing import Any

import psycopg

from screener.config import settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 3


def record(
    *,
    kind: str,
    operation: str,
    actor: str = "system",
    actor_kind: str = "system",
    outcome: str = "ok",
    model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float | Decimal = 0,
    duration_ms: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write one audit row. Never raises.

    Synchronous, like every other database call here. An async caller puts it
    on a worker thread rather than this module growing a second colour.
    """
    import json

    try:
        with psycopg.connect(
            settings().database_url, connect_timeout=CONNECT_TIMEOUT, autocommit=True
        ) as conn:
            conn.execute(
                """
                insert into audit.event (
                    kind, operation, actor, actor_kind, outcome, model,
                    prompt_tokens, completion_tokens, cost_usd, duration_ms, detail
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    kind,
                    operation,
                    actor,
                    actor_kind,
                    outcome,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    Decimal(str(cost_usd)),
                    duration_ms,
                    json.dumps(detail or {}),
                ),
            )
    except Exception as exc:
        # Deliberately broad and deliberately quiet. The caller is in the middle
        # of answering someone; an audit failure is worth a log line and nothing
        # more.
        logger.warning("could not record audit event %s/%s: %s", kind, operation, exc)
