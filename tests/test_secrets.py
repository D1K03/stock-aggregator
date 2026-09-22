import email.message
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from screener.secrets import SecretsError, infisical, load_into_environ, refresh, watch

CLIENT_ID = "machine-identity-id"
CLIENT_SECRET = "machine-identity-secret"
PROJECT_ID = "project-1"
STORED_VALUE = "the-stored-password"
THIRTY_DAYS = 2_592_000


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    """Each test is a process that has loaded nothing, and leaves no trace.

    The module holds what it loaded and the token it loaded it with, exactly
    because a real process must; a test that inherited either would be testing
    the one before it. The environment is put back whole because `refresh()`
    adds and removes names that no `monkeypatch.setenv` ever recorded.
    """
    monkeypatch.setattr(infisical, "_held", infisical._Held())
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


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


# -- keeping them current -------------------------------------------------


class FakeInfisical:
    """Infisical as far as this module can see it: a login and one read.

    `secrets` is what the project holds at the moment of each read, so a test
    edits it between reads the way a person edits the project in the browser.
    """

    def __init__(self, monkeypatch, secrets, *, lifetime=None):
        self.secrets = dict(secrets)
        self.hidden: set[str] = set()
        self.lifetime = lifetime
        self.logins = 0
        self.reads = 0
        self.revoked: set[str] = set()
        self.refuse_reads: int | None = None
        monkeypatch.setattr(
            "screener.secrets.infisical.urllib.request.urlopen", self.urlopen
        )

    def urlopen(self, request: urllib.request.Request, timeout=None):
        if "universal-auth/login" in request.full_url:
            self.logins += 1
            body: dict[str, object] = {"accessToken": f"token-{self.logins}"}
            if self.lifetime is not None:
                body["expiresIn"] = self.lifetime
            return io.BytesIO(json.dumps(body).encode())

        self.reads += 1
        token = (request.get_header("Authorization") or "").removeprefix("Bearer ")
        status = 401 if token in self.revoked else self.refuse_reads
        if status is not None:
            raise urllib.error.HTTPError(
                request.full_url, status, "Refused", email.message.Message(), None
            )
        return io.BytesIO(
            json.dumps(
                {
                    "secrets": [
                        {
                            "secretKey": key,
                            # What Infisical really sends in place of a value a
                            # role may list but not read.
                            "secretValue": "<hidden-by-infisical>"
                            if key in self.hidden
                            else value,
                            "secretValueHidden": key in self.hidden,
                        }
                        for key, value in self.secrets.items()
                    ]
                }
            ).encode()
        )


def test_a_value_edited_in_infisical_reaches_the_running_process(monkeypatch):
    # The whole point: an edit in the browser, and no restart.
    _configure(monkeypatch)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "first"})
    load_into_environ()

    project.secrets["SCREENER_KEY"] = "second"

    assert refresh() == ["SCREENER_KEY"]
    assert os.environ["SCREENER_KEY"] == "second"


def test_an_unchanged_project_changes_nothing(monkeypatch):
    _configure(monkeypatch)
    FakeInfisical(monkeypatch, {"SCREENER_KEY": "first"})
    load_into_environ()

    assert refresh() == []
    assert os.environ["SCREENER_KEY"] == "first"


def test_a_name_added_in_infisical_is_added_here(monkeypatch):
    # How a feature is switched on: EDGAR_CONTACT_EMAIL arrives, and the next
    # pass reads it.
    _configure(monkeypatch)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "first"})
    load_into_environ()

    project.secrets["SCREENER_NEW"] = "arrived"

    assert refresh() == ["SCREENER_NEW"]
    assert os.environ["SCREENER_NEW"] == "arrived"


def test_a_name_deleted_in_infisical_is_removed_here(monkeypatch):
    # And how one is switched off: unset is the off switch for several things
    # here, and it should not wait for a deploy.
    _configure(monkeypatch)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "first", "SCREENER_GONE": "x"})
    load_into_environ()

    del project.secrets["SCREENER_GONE"]

    assert refresh() == ["SCREENER_GONE"]
    assert "SCREENER_GONE" not in os.environ
    assert os.environ["SCREENER_KEY"] == "first"


def test_a_name_the_container_set_still_wins_after_boot(monkeypatch):
    # The boot rule, kept: a deliberate override is not replaced at startup,
    # and it must not be replaced a minute later either.
    _configure(monkeypatch)
    monkeypatch.setenv("SCREENER_KEY", "the-override")
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "stored"})
    load_into_environ()

    project.secrets["SCREENER_KEY"] = "stored-and-edited"

    assert refresh() == []
    assert os.environ["SCREENER_KEY"] == "the-override"


def test_an_empty_answer_is_refused_rather_than_unsetting_everything(monkeypatch):
    # No deployment of this project has an empty environment, so an empty
    # answer is a fault, and applying it would unset every credential in every
    # container at once.
    _configure(monkeypatch)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "first"})
    load_into_environ()

    project.secrets.clear()

    with pytest.raises(SecretsError, match="no secrets at all"):
        refresh()
    assert os.environ["SCREENER_KEY"] == "first"


def test_hidden_values_are_fatal_at_boot_rather_than_loaded(monkeypatch):
    # A role that may list secrets but not read them is answered with a
    # placeholder, not an error. Loaded, it would start every container with
    # `<hidden-by-infisical>` as its token, its password and its key.
    _configure(monkeypatch)
    monkeypatch.delenv("SCREENER_KEY", raising=False)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": STORED_VALUE})
    project.hidden.add("SCREENER_KEY")

    with pytest.raises(SecretsError, match="SCREENER_KEY.*Viewer"):
        load_into_environ()
    assert "SCREENER_KEY" not in os.environ


def test_hidden_values_on_a_later_read_change_nothing(monkeypatch):
    # A role downgraded under a running process: it keeps what it has.
    _configure(monkeypatch)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "first"})
    load_into_environ()

    project.secrets["SCREENER_KEY"] = "second"
    project.hidden.add("SCREENER_KEY")

    with pytest.raises(SecretsError, match="hid the value"):
        refresh()
    assert os.environ["SCREENER_KEY"] == "first"


def test_the_token_is_kept_between_reads(monkeypatch):
    # Logins are rate-limited by address, and the box is one address for every
    # stack on it. A thirty-day token is not worth replacing once a minute.
    _configure(monkeypatch)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "first"}, lifetime=THIRTY_DAYS)
    load_into_environ()

    for _ in range(3):
        refresh()

    assert project.logins == 1
    assert project.reads == 4


def test_a_token_without_a_lifetime_is_used_once(monkeypatch):
    _configure(monkeypatch)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "first"})
    load_into_environ()

    refresh()

    assert project.logins == 2


def test_a_revoked_token_is_replaced_rather_than_failing_the_read(monkeypatch):
    _configure(monkeypatch)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "first"}, lifetime=THIRTY_DAYS)
    load_into_environ()

    project.revoked.add("token-1")
    project.secrets["SCREENER_KEY"] = "second"

    assert refresh() == ["SCREENER_KEY"]
    assert project.logins == 2


def _watching(monkeypatch, on_change):
    """Start `watch()` polling far faster than production, and a way to stop it."""
    monkeypatch.setattr(infisical, "_refresh_seconds", lambda: 0.01)
    stop = threading.Event()
    thread = watch(on_change=on_change, stop=stop)
    assert thread is not None
    return stop, thread


def test_watch_hears_a_change_and_says_which_names_never_which_values(monkeypatch, caplog):
    _configure(monkeypatch)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "first"})
    load_into_environ()

    heard: list[list[str]] = []
    arrived = threading.Event()

    def on_change(names):
        heard.append(names)
        arrived.set()

    with caplog.at_level(logging.INFO, logger="screener.secrets.infisical"):
        stop, thread = _watching(monkeypatch, on_change)
        project.secrets["SCREENER_KEY"] = STORED_VALUE
        try:
            assert arrived.wait(5)
        finally:
            stop.set()
            thread.join(5)

    assert heard[0] == ["SCREENER_KEY"]
    assert os.environ["SCREENER_KEY"] == STORED_VALUE
    assert "SCREENER_KEY" in caplog.text
    assert STORED_VALUE not in caplog.text


def test_watch_waits_out_a_failed_read_rather_than_dying(monkeypatch, caplog):
    # An Infisical outage must not take the process down: it is running on
    # values that worked, and the boot it would restart into is the one read
    # that really is fatal.
    _configure(monkeypatch)
    project = FakeInfisical(monkeypatch, {"SCREENER_KEY": "first"})
    load_into_environ()
    project.refuse_reads = 503

    arrived = threading.Event()
    stop, thread = _watching(monkeypatch, lambda names: arrived.set())
    try:
        deadline = time.monotonic() + 5
        while project.reads < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert project.reads >= 3
        project.refuse_reads = None
        project.secrets["SCREENER_KEY"] = "second"
        assert arrived.wait(5)
    finally:
        stop.set()
        thread.join(5)

    assert os.environ["SCREENER_KEY"] == "second"
    assert "keeping what is loaded" in caplog.text


def test_nothing_is_watched_without_a_machine_identity(monkeypatch):
    # Local development and CI: nothing to watch, and no thread started.
    for name in ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET", "INFISICAL_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)

    assert watch() is None


def test_watching_can_be_switched_off(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("INFISICAL_REFRESH_SECONDS", "0")

    assert watch() is None
