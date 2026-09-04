"""Reads secrets from Infisical using a machine identity.

Deliberately stdlib `urllib` rather than the Infisical SDK or CLI. This module
runs before anything else in the process, so a dependency here is a dependency
in the container image and a failure mode during startup; two HTTP calls do not
justify either. The same reasoning rules out installing the CLI, which would
mean an apt repository or a piped install script in the Dockerfile.

Values are written into `os.environ` and never to disk. The only credentials
stored on the server are the three that authenticate this exchange.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://app.infisical.com"
TIMEOUT_SECONDS = 20


class SecretsError(RuntimeError):
    """Infisical was configured but could not be read."""


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
        raise SecretsError(f"Infisical returned HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SecretsError(f"Infisical request to {url} failed: {exc}") from exc


def fetch(
    client_id: str,
    client_secret: str,
    project_id: str,
    environment: str,
    host: str = DEFAULT_HOST,
) -> dict[str, str]:
    """Authenticate with the machine identity and read one environment."""
    login = _request(
        f"{host}/api/v1/auth/universal-auth/login",
        data=json.dumps({"clientId": client_id, "clientSecret": client_secret}).encode(),
    )
    token = login.get("accessToken")
    if not token:
        raise SecretsError("Infisical login returned no access token")

    body = _request(
        f"{host}/api/v3/secrets/raw"
        f"?workspaceId={project_id}&environment={environment}&secretPath=/",
        token=token,
    )
    return {
        secret["secretKey"]: secret["secretValue"] for secret in body.get("secrets", [])
    }


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
    client_id = os.environ.get("INFISICAL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("INFISICAL_CLIENT_SECRET", "").strip()
    project_id = os.environ.get("INFISICAL_PROJECT_ID", "").strip()

    if not (client_id and client_secret and project_id):
        logger.info("no Infisical machine identity configured; using the environment as-is")
        return 0

    environment = os.environ.get("INFISICAL_ENV", "prod").strip() or "prod"
    host = os.environ.get("INFISICAL_HOST", "").strip() or DEFAULT_HOST

    secrets = fetch(client_id, client_secret, project_id, environment, host)

    applied = [key for key in secrets if key not in os.environ]
    for key in applied:
        os.environ[key] = secrets[key]

    # Names only. A value has never been logged by this module and must not be.
    logger.info(
        "loaded %d secret(s) from Infisical (%s): %s",
        len(applied),
        environment,
        ", ".join(sorted(applied)) or "none",
    )
    return len(applied)
