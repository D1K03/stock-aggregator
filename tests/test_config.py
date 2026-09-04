import pytest

from screener.ai.config import RouterConfig
from screener.config import Settings, settings
from screener.config import env
from screener.fetch.config import ProxyConfig
from screener.notify.config import DiscordConfig

CREDENTIAL = "hunter2-do-not-print-me"


def test_a_missing_database_url_names_the_variable_that_is_missing(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        settings()


def test_a_database_url_exported_as_an_empty_string_counts_as_unset(monkeypatch):
    # This is what a shell produces from an unset compose interpolation such as
    # ${DATABASE_URL:-}. Treating it as a configured empty value would turn a
    # broken deploy into a confusing psycopg error much further downstream.
    monkeypatch.setenv("DATABASE_URL", "   ")
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        settings()


def test_an_integer_variable_with_a_typo_raises_rather_than_using_the_default(
    monkeypatch,
):
    # Falling back silently would route proxy traffic at a port nobody chose.
    monkeypatch.setenv("SCREENER_TEST_PORT", "444o5")
    with pytest.raises(RuntimeError, match="must be an integer"):
        env.integer("SCREENER_TEST_PORT", 44445)


@pytest.mark.parametrize(
    "config",
    [
        Settings(database_url=CREDENTIAL),
        ProxyConfig(proxy=CREDENTIAL, user=CREDENTIAL, password=CREDENTIAL),
        ProxyConfig(api_key=CREDENTIAL),
        RouterConfig(api_key=CREDENTIAL),
        DiscordConfig(webhook_url=CREDENTIAL),
    ],
)
def test_no_config_object_prints_its_credentials(config):
    # A frozen dataclass repr appears in every traceback that carries the
    # object, and tracebacks reach log aggregators. Each credential field is
    # declared repr=False; this is what notices when a new one is not.
    assert CREDENTIAL not in repr(config)


def test_bright_data_is_not_configured_when_nothing_is_set(monkeypatch):
    for name in (
        "BRIGHTDATA_PROXY",
        "BRIGHTDATA_PROXY_USER",
        "BRIGHTDATA_PROXY_PASS",
        "BRIGHTDATA_API_KEY",
        "BRIGHTDATA_UNLOCKER_ZONE",
    ):
        monkeypatch.delenv(name, raising=False)
    config = ProxyConfig.from_env()
    assert config.proxy_url() is None
    assert config.unlocker_enabled is False


def test_the_combined_proxy_string_survives_a_password_containing_colons(monkeypatch):
    # Bright Data hands out host:port:user:pass, and their passwords do contain
    # colons — splitting on every colon silently truncates the credential.
    monkeypatch.setenv("BRIGHTDATA_PROXY", "brd.superproxy.io:44445:the-user:a:b:c")
    assert ProxyConfig.from_env().proxy_url() == (
        "http://the-user:a:b:c@brd.superproxy.io:44445"
    )


def test_a_session_suffix_is_appended_to_the_username_not_the_password(monkeypatch):
    # The session is how Bright Data picks an exit IP, and it only works when
    # it rides on the username.
    monkeypatch.setenv("BRIGHTDATA_PROXY", "host:1:user:pass")
    assert ProxyConfig.from_env().proxy_url(session="beef") == (
        "http://user-session-beef:pass@host:1"
    )


def test_a_username_that_already_pins_a_session_is_left_alone(monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_PROXY", "host:1:user-session-fixed:pass")
    assert ProxyConfig.from_env().proxy_url(session="beef") == (
        "http://user-session-fixed:pass@host:1"
    )
