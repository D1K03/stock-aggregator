"""Asking the magpie container for a document. `httpx` and nothing more.

This is what the status service and the bot import. Neither should pay for lxml
to ask for an article, which is the same split `screener.transcribe` draws
between its client and the container that holds the model.
"""

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://magpie:8082"

# A scrape is a robots fetch, then up to three page fetches, then extraction.
# Generous, because the alternative to waiting is asking again, and asking again
# is what costs money.
TIMEOUT = 90.0


@dataclass(frozen=True, slots=True)
class Scraped:
    """What came back. Exactly one of `document` and `reason` is set."""

    document: dict | None = None
    reason: str | None = None
    detail: str = ""
    attempts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.document is not None


def magpie_url() -> str:
    return os.environ.get("MAGPIE_URL", DEFAULT_URL).rstrip("/")


def enabled() -> bool:
    """False when no scraper is configured, which is how this is switched off."""
    return bool(os.environ.get("MAGPIE_URL", DEFAULT_URL))


def scrape(url: str, *, requested_by: str | None = None, client: httpx.Client | None = None) -> Scraped:
    """Ask for one document. Never raises.

    A refusal is a result rather than an exception, because every caller wants
    to say why rather than to handle an error — and a scraper whose ordinary
    outcomes are exceptions gets wrapped in a bare `except` by its second caller.
    """
    payload = {"url": url, "requested_by": requested_by}
    try:
        if client is not None:
            response = client.post("/scrape", json=payload, timeout=TIMEOUT)
        else:
            with httpx.Client(base_url=magpie_url(), timeout=TIMEOUT) as owned:
                response = owned.post("/scrape", json=payload)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        logger.warning("magpie is not answering: %s", type(exc).__name__)
        return Scraped(reason="unavailable", detail="the scraper is not reachable")

    if body.get("document"):
        return Scraped(document=body["document"])
    return Scraped(
        reason=body.get("reason", "failed"),
        detail=body.get("detail", ""),
        attempts=tuple(body.get("attempts") or ()),
    )
