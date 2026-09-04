import httpx
import pytest

from screener.notify import Alert, AlertField, ChannelError, DiscordWebhook, NotificationChannel

WEBHOOK = "https://discord.test/api/webhooks/1/token"


def recording_transport(*responses):
    seen: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return remaining.pop(0) if remaining else httpx.Response(200, json={})

    return httpx.MockTransport(handler), seen


def test_a_webhook_satisfies_the_channel_protocol():
    # The point of the protocol is that Telegram, Signal or a gateway bot
    # arrive as another implementation and nothing above them changes.
    channel = DiscordWebhook(WEBHOOK, transport=recording_transport()[0])
    assert isinstance(channel, NotificationChannel)


def test_an_alert_is_sent_as_an_embed_with_its_fields():
    transport, seen = recording_transport(httpx.Response(200, json={}))
    alert = Alert(
        title="NVDA 68 -> 82",
        body="Driver: three upward revisions in 5 days.",
        fields=(AlertField("Quality", "91"), AlertField("Valuation", "44")),
    )
    DiscordWebhook(WEBHOOK, transport=transport).send(alert)

    assert len(seen) == 1
    import json

    payload = json.loads(seen[0].content)
    embed = payload["embeds"][0]
    assert embed["title"] == "NVDA 68 -> 82"
    assert [f["name"] for f in embed["fields"]] == ["Quality", "Valuation"]
    # wait=true makes Discord validate and persist before answering, so a
    # malformed payload is a 400 rather than a 204 followed by silence.
    assert "wait=true" in str(seen[0].url)


def test_a_rate_limited_send_is_retried_once_using_the_servers_own_delay(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("screener.notify.discord.time.sleep", slept.append)

    transport, seen = recording_transport(
        httpx.Response(429, json={"retry_after": 0.25}),
        httpx.Response(200, json={}),
    )
    DiscordWebhook(WEBHOOK, transport=transport).send(Alert(title="t"))

    assert slept == [0.25]
    assert len(seen) == 2


def test_a_retry_that_is_also_rate_limited_raises_rather_than_looping(monkeypatch):
    monkeypatch.setattr("screener.notify.discord.time.sleep", lambda _: None)
    transport, seen = recording_transport(
        httpx.Response(429, json={"retry_after": 0.1}),
        httpx.Response(429, json={"retry_after": 0.1}),
    )
    with pytest.raises(ChannelError, match="429"):
        DiscordWebhook(WEBHOOK, transport=transport).send(Alert(title="t"))
    assert len(seen) == 2


def test_a_rejected_payload_raises_rather_than_dropping_the_alert():
    # Returning a status a caller might forget to check would drop alerts
    # silently, and a muted channel is indistinguishable from a quiet market.
    transport, _ = recording_transport(httpx.Response(400, json={}))
    with pytest.raises(ChannelError, match="400"):
        DiscordWebhook(WEBHOOK, transport=transport).send(Alert(title="t"))


def test_an_unconfigured_webhook_raises_at_construction(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(ChannelError, match="DISCORD_WEBHOOK_URL is not set"):
        DiscordWebhook()


def test_a_long_body_is_truncated_rather_than_rejected_wholesale():
    transport, seen = recording_transport(httpx.Response(200, json={}))
    DiscordWebhook(WEBHOOK, transport=transport).send(Alert(title="t", body="x" * 9000))

    import json

    embed = json.loads(seen[0].content)["embeds"][0]
    assert len(embed["description"]) == 4096
