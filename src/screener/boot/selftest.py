"""Checks every configured integration and reports what it found.

Nothing in the infrastructure layer has a consumer yet — ingest, scoring and
alerting are all unwritten — so a root can be quietly broken for months and
nothing would say so. This is what closes that gap: one command, run on the
box after a deploy, that exercises each piece against the real world.

Anything unconfigured is reported as SKIP rather than failing, because "Bright
Data is switched off" is the expected state, not a fault. Discord is checked
for configuration only and never sent to: posting into a real channel is an
outward-facing action and a self-test should not make one.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from screener.ai import complete
from screener.ai.config import RouterConfig
from screener.bot.config import BotConfig
from screener.fetch import fetch
from screener.fetch.config import ProxyConfig
from screener.notify.config import DiscordConfig
from screener.health import checks
from screener.provenance import GIT_SHA_ENV, git_sha

logger = logging.getLogger(__name__)

# Returns the caller's public IP as plain text, so the direct and proxied
# checks can be compared: if they match, traffic never left the box and the
# proxy is not doing what it is being paid for.
IP_ECHO_URL = "https://api.ipify.org"

OK, SKIP, FAIL = "OK", "SKIP", "FAIL"


@dataclass(slots=True)
class Check:
    name: str
    outcome: str
    detail: str


def _database() -> Check:
    reason, migrations = checks.database()
    if reason == "ok":
        return Check("database", OK, f"{migrations} migrations applied")
    if reason == "no schema":
        return Check("database", FAIL, "reachable but no migrations applied")
    if reason == "unconfigured":
        return Check("database", FAIL, "DATABASE_URL is not set")
    return Check("database", FAIL, reason)


def _build() -> Check:
    sha = git_sha()
    if sha == "unknown":
        return Check("build", FAIL, f"{GIT_SHA_ENV} is not set and no git checkout")
    return Check("build", OK, sha[:12])


def _direct() -> Check:
    try:
        result = fetch(IP_ECHO_URL, ("direct",), timeout=20.0)
    except Exception as exc:
        return Check("fetch direct", FAIL, f"{type(exc).__name__}: {exc}")
    return Check("fetch direct", OK, f"exit IP {result.text.strip()}")


def _isp_proxy(direct: Check) -> Check:
    if ProxyConfig.from_env().proxy_url() is None:
        return Check("fetch isp_proxy", SKIP, "no Bright Data proxy credentials")
    try:
        result = fetch(IP_ECHO_URL, ("isp_proxy",), timeout=30.0)
    except Exception as exc:
        return Check("fetch isp_proxy", FAIL, f"{type(exc).__name__}: {exc}")

    proxied = result.text.strip()
    if direct.outcome == OK and proxied in direct.detail:
        # The request succeeded but came out of the same address, so the proxy
        # is configured, billed, and doing nothing.
        return Check("fetch isp_proxy", FAIL, f"exit IP {proxied} matches direct")
    return Check("fetch isp_proxy", OK, f"exit IP {proxied}")


def _openrouter() -> Check:
    if not RouterConfig.from_env().enabled:
        return Check("openrouter", SKIP, "OPENROUTER_API_KEY is not set")
    try:
        # Deliberately tiny. This proves the key, the model id and the billing
        # path, and costs a fraction of a penny to do it.
        completion = complete(
            system="Reply with exactly one word.",
            user="Say: ok",
            max_tokens=5,
            temperature=0.0,
        )
    except Exception as exc:
        return Check("openrouter", FAIL, f"{type(exc).__name__}: {exc}")
    return Check(
        "openrouter",
        OK,
        f"{completion.model} responded, ${completion.cost_usd:.5f}",
    )


def _discord() -> Check:
    if not DiscordConfig.from_env().enabled:
        return Check("discord", SKIP, "DISCORD_WEBHOOK_URL is not set")
    return Check("discord", OK, "webhook configured (not sent)")


def _safe(name: str, check: Callable[[], Check]) -> Check:
    """Run one check, turning any unexpected exception into a FAIL.

    A self-test that crashes on its second check tells you less than one that
    reports six results, and an unset DATABASE_URL should not stop the Bright
    Data check from running.
    """
    try:
        return check()
    except Exception as exc:
        return Check(name, FAIL, f"{type(exc).__name__}: {exc}")


def _bot() -> Check:
    """Whether the bot's token is real and its allow-list is usable.

    Asks Discord who the token belongs to rather than only checking that a
    token exists. That is a read, so it posts nothing and cannot be noticed in
    the server, but it is the difference between "a token is configured" and
    "the token works" — which is the whole reason this command exists.

    Importing `BotConfig` does not pull in discord.py, so the unconfigured case
    stays as cheap as every other check here.
    """
    config = BotConfig.from_env()
    if not config.enabled:
        return Check("discord bot", SKIP, "DISCORD_BOT_TOKEN is not set")
    if config.guild_id is None:
        # Without a guild, commands register globally and take about an hour to
        # appear, which reads as a broken bot to anyone testing after a deploy.
        return Check("discord bot", FAIL, "DISCORD_GUILD_ID is not set")
    if not config.allowed_user_ids:
        return Check("discord bot", FAIL, "ALLOWED_DISCORD_USER_IDS is empty")

    try:
        result = fetch(
            "https://discord.com/api/v10/users/@me",
            ("direct",),
            timeout=15.0,
            headers={
                "Authorization": f"Bot {config.token}",
                # Discord asks bots to identify themselves honestly, so the
                # browser fingerprint the fetch layer sends by default is the
                # wrong claim to make to this particular API.
                "User-Agent": (
                    "DiscordBot (https://github.com/D1K03/stock-aggregator, 0.1.0)"
                ),
            },
        )
    except Exception as exc:
        return Check("discord bot", FAIL, f"{type(exc).__name__}: {exc}")

    username = result.json().get("username", "?")
    return Check(
        "discord bot",
        OK,
        f"{username}, guild {config.guild_id}, "
        f"{len(config.allowed_user_ids)} permitted user(s)",
    )


def run() -> bool:
    """Run every check and log the results. True when nothing failed."""
    direct = _safe("fetch direct", _direct)
    results = [
        _safe("database", _database),
        _safe("build", _build),
        direct,
        _safe("fetch isp_proxy", lambda: _isp_proxy(direct)),
        _safe("openrouter", _openrouter),
        _safe("discord", _discord),
        _safe("discord bot", _bot),
    ]

    width = max(len(c.name) for c in results)
    for check in results:
        logger.info("%-4s %-*s  %s", check.outcome, width, check.name, check.detail)

    failed = [c.name for c in results if c.outcome == FAIL]
    if failed:
        logger.error("selftest failed: %s", ", ".join(failed))
        return False
    logger.info("selftest passed")
    return True
