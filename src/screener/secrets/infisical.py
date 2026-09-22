"""Reads secrets from Infisical using a machine identity, and keeps them current.

Deliberately stdlib `urllib` rather than the Infisical SDK or CLI. This module
runs before anything else in the process, so a dependency here is a dependency
in the container image and a failure mode during startup; two HTTP calls do not
justify either. The same reasoning rules out installing the CLI, which would
mean an apt repository or a piped install script in the Dockerfile.

Values are written into `os.environ` and never to disk. The only credentials
stored on the server are the three that authenticate this exchange.

`watch()` reads them again every minute and writes whatever changed into the
same `os.environ`, so a value edited in Infisical reaches a running process
without a restart. That works only because nothing downstream holds on to what
it read: every config object in the tree is built when it is used, the rule
`screener.config.settings` states for the database URL, which this makes
load-bearing for every credential. A worker reads its configuration at the top
of each pass rather than once at boot, and the bot, whose token and guild are
fixed for the life of a gateway session, reconnects.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://app.infisical.com"
TIMEOUT_SECONDS = 20

# How often `watch()` asks, and so how long an edit takes to arrive. Seven
# processes asking once a minute is seven reads a minute from the box, against a
# free-plan allowance of 120 a minute per address. `INFISICAL_REFRESH_SECONDS=0`
# switches watching off and puts every process back on the values it booted with.
DEFAULT_REFRESH_SECONDS = 60

# A held token is replaced this long before Infisical says it expires, so a read
# never races the deadline.
TOKEN_MARGIN_SECONDS = 300


class SecretsError(RuntimeError):
    """Infisical was configured but could not be read."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class _Identity:
    client_id: str
    client_secret: str = field(repr=False)
    project_id: str
    environment: str
    host: str = DEFAULT_HOST


@dataclass
class _Held:
    """What this process took from Infisical, and the token it read it with."""

    # Names this process may replace. Anything else in the environment was put
    # there by the container and wins -- at boot, where a deliberate
    # `docker compose run -e ...` override must survive, and after it.
    owned: set[str] = field(default_factory=set)
    token: str | None = field(default=None, repr=False)
    token_until: float | None = None


_held = _Held()
_lock = threading.Lock()


def _identity() -> _Identity | None:
    """The machine identity from the environment, or None when it is not all there."""
    client_id = os.environ.get("INFISICAL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("INFISICAL_CLIENT_SECRET", "").strip()
    project_id = os.environ.get("INFISICAL_PROJECT_ID", "").strip()
    if not (client_id and client_secret and project_id):
        return None
    return _Identity(
        client_id=client_id,
        client_secret=client_secret,
        project_id=project_id,
        environment=os.environ.get("INFISICAL_ENV", "prod").strip() or "prod",
        host=os.environ.get("INFISICAL_HOST", "").strip() or DEFAULT_HOST,
    )


def _request(url: str, *, data: bytes | None = None, token: str | None = None) -> Any:
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The body may carry the reason (bad identity, wrong project) but may
        # also carry secret material, so it is not logged — only the status.
        raise SecretsError(
            f"Infisical returned HTTP {exc.code} for {url}", status=exc.code
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SecretsError(f"Infisical request to {url} failed: {exc}") from exc


def _login(identity: _Identity) -> tuple[str, float | None]:
    """An access token, and when to stop using it: None means use it once."""
    login = _request(
        f"{identity.host}/api/v1/auth/universal-auth/login",
        data=json.dumps(
            {"clientId": identity.client_id, "clientSecret": identity.client_secret}
        ).encode(),
    )
    token = login.get("accessToken")
    if not token:
        raise SecretsError("Infisical login returned no access token")
    lifetime = login.get("expiresIn")
    if not isinstance(lifetime, (int, float)) or lifetime <= TOKEN_MARGIN_SECONDS:
        return token, None
    return token, time.monotonic() + lifetime - TOKEN_MARGIN_SECONDS


def _read(identity: _Identity, token: str) -> dict[str, str]:
    body = _request(
        f"{identity.host}/api/v3/secrets/raw"
        f"?workspaceId={identity.project_id}&environment={identity.environment}"
        "&secretPath=/",
        token=token,
    )
    secrets = body.get("secrets", [])
    # A role that may list secrets but not read them is not refused. It is
    # answered with every value replaced by the text `<hidden-by-infisical>`,
    # which would otherwise go into the environment as though it were the token,
    # the password and the key -- a container that starts cleanly and fails at
    # everything, which is the failure this project most wants to be loud.
    hidden = sorted(s["secretKey"] for s in secrets if s.get("secretValueHidden"))
    if hidden:
        raise SecretsError(
            f"Infisical hid the value of {len(hidden)} secret(s) "
            f"({', '.join(hidden)}): the machine identity can list them but not "
            "read them. Give it a role that reads secret values, such as Viewer"
        )
    return {secret["secretKey"]: secret["secretValue"] for secret in secrets}


def _current(identity: _Identity) -> dict[str, str]:
    """Read one environment, logging in only when no usable token is held.

    Infisical limits logins by address, and every stack on this box shares one
    address -- two others read the same Infisical -- so logging in on every poll
    would spend that allowance seven times a minute to replace a token that
    lasts thirty days. A 401 means it was revoked early, and is answered by
    logging in again rather than by failing the read.
    """
    token = _held.token
    until = _held.token_until
    if token is not None and until is not None and time.monotonic() < until:
        try:
            return _read(identity, token)
        except SecretsError as exc:
            if exc.status != 401:
                raise
    token, until = _login(identity)
    _held.token, _held.token_until = token, until
    return _read(identity, token)


def fetch(
    client_id: str,
    client_secret: str,
    project_id: str,
    environment: str,
    host: str = DEFAULT_HOST,
) -> dict[str, str]:
    """Authenticate with the machine identity and read one environment."""
    identity = _Identity(client_id, client_secret, project_id, environment, host)
    token, _ = _login(identity)
    return _read(identity, token)


def load_into_environ() -> int:
    """Load secrets into `os.environ`, returning how many were applied.

    Missing credentials is a no-op returning 0, not an error: that is how local
    development and CI run unstubbed, reading a `.env` or exported variables as
    normal. A *failed* fetch is fatal, because starting with half a
    configuration is worse than not starting at all.

    Existing environment variables win. That keeps a deliberate override — a
    one-off `docker compose run -e ...` while debugging — from being silently
    replaced by the stored value.
    """
    identity = _identity()
    if identity is None:
        logger.info("no Infisical machine identity configured; using the environment as-is")
        return 0

    with _lock:
        secrets = _current(identity)
        applied = [key for key in secrets if key not in os.environ]
        for key in applied:
            os.environ[key] = secrets[key]
        _held.owned.update(applied)

    # Names only. A value has never been logged by this module and must not be.
    logger.info(
        "loaded %d secret(s) from Infisical (%s): %s",
        len(applied),
        identity.environment,
        ", ".join(sorted(applied)) or "none",
    )
    return len(applied)


def refresh() -> list[str]:
    """Bring `os.environ` into line with Infisical, returning the names that changed.

    Only what this process took from Infisical is replaced; a name the container
    set itself still wins, as it did at boot. A name new in Infisical is added
    and one deleted there is removed, because unsetting a variable is how several
    things here are switched off and that should not wait for a restart.

    Except when Infisical answers with nothing at all. No deployment of this
    project has an empty environment, so an empty answer is a fault somewhere
    between here and there, and applying it would unset every credential in every
    container within the minute. It is refused, and said.

    New values are written one after another, so a reader in the middle of the
    loop can see a new access key beside the old secret for the microseconds
    between them. That costs one failed request, which is cheaper than a lock
    every configuration read in the tree would have to take.
    """
    identity = _identity()
    if identity is None:
        return []

    with _lock:
        secrets = _current(identity)
        owned = _held.owned
        if not secrets and owned:
            raise SecretsError(
                "Infisical returned no secrets at all; keeping the ones already loaded"
            )
        changed: list[str] = []
        for key, value in secrets.items():
            if key in owned:
                if os.environ.get(key) != value:
                    os.environ[key] = value
                    changed.append(key)
            elif key not in os.environ:
                os.environ[key] = value
                owned.add(key)
                changed.append(key)
        for key in owned - secrets.keys():
            os.environ.pop(key, None)
            owned.discard(key)
            changed.append(key)
    return sorted(changed)


def watch(
    on_change: Callable[[list[str]], None] | None = None,
    *,
    stop: threading.Event | None = None,
) -> threading.Thread | None:
    """Keep this process's secrets current, from a daemon thread.

    Every `INFISICAL_REFRESH_SECONDS` (60 unless set) it calls `refresh()` and
    logs the names that changed, never a value. `on_change` hears those names,
    for a caller holding something a new value cannot reach by being read again:
    the bot's gateway session.

    A read that fails is logged and tried again next time, never raised. The
    process is already running on values that worked, and making an Infisical
    outage fatal here would turn it into a restart loop, with every container
    then failing the one fetch that really is fatal: the one at boot.

    Returns the thread, or None when there is nothing to watch: no machine
    identity (local development, CI) or watching switched off. `stop` ends it,
    for tests; production lets it die with the process.
    """
    if _identity() is None:
        return None
    interval = _refresh_seconds()
    if interval <= 0:
        logger.info("INFISICAL_REFRESH_SECONDS is 0; secrets are read once, at startup")
        return None
    halt = stop or threading.Event()

    def run() -> None:
        while not halt.wait(interval):
            try:
                changed = refresh()
            except SecretsError as exc:
                logger.warning("could not re-read Infisical, keeping what is loaded: %s", exc)
                continue
            except Exception:
                # A watcher that dies quietly is a process that has stopped
                # following Infisical while looking exactly as it did before.
                logger.exception("reading Infisical failed; trying again next time")
                continue
            if not changed:
                continue
            logger.info(
                "updated %d secret(s) from Infisical: %s", len(changed), ", ".join(changed)
            )
            if on_change is not None:
                try:
                    on_change(changed)
                except Exception:
                    logger.exception("acting on the changed secrets failed")

    thread = threading.Thread(target=run, name="infisical-watch", daemon=True)
    thread.start()
    logger.info("watching Infisical for changes every %ds", interval)
    return thread


def _refresh_seconds() -> int:
    raw = os.environ.get("INFISICAL_REFRESH_SECONDS", "").strip()
    if not raw:
        return DEFAULT_REFRESH_SECONDS
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "INFISICAL_REFRESH_SECONDS=%r is not a whole number of seconds; using %d",
            raw,
            DEFAULT_REFRESH_SECONDS,
        )
        return DEFAULT_REFRESH_SECONDS
