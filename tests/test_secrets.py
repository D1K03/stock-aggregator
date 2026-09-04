import email.message
import io
import json
import logging
import urllib.error

import pytest

from screener.secrets import SecretsError, load_into_environ

CLIENT_ID = "machine-identity-id"
CLIENT_SECRET = "machine-identity-secret"
PROJECT_ID = "project-1"
STORED_VALUE = "the-stored-password"


def _configure(monkeypatch):
    monkeypatch.setenv("INFISICAL_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("INFISICAL_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("INFISICAL_PROJECT_ID", PROJECT_ID)


def _respond_with(monkeypatch, secrets):
    """Stub urlopen: the first call is the login, the second reads secrets."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        if "universal-auth/login" in request.full_url:
            body = {"accessToken": "a-token"}
        else:
            body = {
                "secrets": [
                    {"secretKey": k, "secretValue": v} for k, v in secrets.items()
                ]
            }
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(
        "screener.secrets.infisical.urllib.request.urlopen", fake_urlopen
    )
    return calls


def test_no_machine_identity_is_a_no_op_rather_than_an_error(monkeypatch):
    # This is how local development and CI run unstubbed: no credentials means
    # the environment is used exactly as it already is.
    for name in ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET", "INFISICAL_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert load_into_environ() == 0


def test_a_partial_machine_identity_is_also_a_no_op(monkeypatch):
    monkeypatch.setenv("INFISICAL_CLIENT_ID", CLIENT_ID)
    monkeypatch.delenv("INFISICAL_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("INFISICAL_PROJECT_ID", raising=False)
    assert load_into_environ() == 0


def test_fetched_secrets_are_written_into_the_environment(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.delenv("SCREENER_FETCHED", raising=False)
    _respond_with(monkeypatch, {"SCREENER_FETCHED": STORED_VALUE})

    import os

    assert load_into_environ() == 1
    assert os.environ["SCREENER_FETCHED"] == STORED_VALUE


def test_an_existing_environment_variable_is_not_overwritten(monkeypatch):
    # A deliberate `docker compose run -e ...` override while debugging must
    # survive, or the stored value silently wins and the debugging is a lie.
    _configure(monkeypatch)
    monkeypatch.setenv("SCREENER_FETCHED", "the-override")
    _respond_with(monkeypatch, {"SCREENER_FETCHED": STORED_VALUE})

    import os

    assert load_into_environ() == 0
    assert os.environ["SCREENER_FETCHED"] == "the-override"


def test_a_failed_fetch_is_fatal_rather_than_falling_back(monkeypatch):
    # Starting with half a configuration means failing later, somewhere less
    # obvious, possibly against the wrong database.
    _configure(monkeypatch)

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", email.message.Message(), None
        )

    monkeypatch.setattr(
        "screener.secrets.infisical.urllib.request.urlopen", fake_urlopen
    )
    with pytest.raises(SecretsError, match="HTTP 403"):
        load_into_environ()


def test_a_login_without_a_token_is_reported_rather_than_used(monkeypatch):
    _configure(monkeypatch)

    def fake_urlopen(request, timeout=None):
        return io.BytesIO(json.dumps({}).encode())

    monkeypatch.setattr(
        "screener.secrets.infisical.urllib.request.urlopen", fake_urlopen
    )
    with pytest.raises(SecretsError, match="no access token"):
        load_into_environ()


def test_secret_values_are_never_logged(monkeypatch, caplog):
    _configure(monkeypatch)
    monkeypatch.delenv("SCREENER_FETCHED", raising=False)
    _respond_with(monkeypatch, {"SCREENER_FETCHED": STORED_VALUE})

    with caplog.at_level(logging.DEBUG):
        load_into_environ()

    assert STORED_VALUE not in caplog.text
    assert CLIENT_SECRET not in caplog.text
    # The names are logged, deliberately: knowing which keys arrived is how you
    # diagnose a half-populated project.
    assert "SCREENER_FETCHED" in caplog.text
