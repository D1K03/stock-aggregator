import asyncio
import types
from collections.abc import Callable, Coroutine
from typing import Any, cast

import httpx
import pytest
from discord import app_commands

from screener.ai import AiError
from screener.ai.models import MODELS, resolve_model
from screener.bot import agent
from screener.bot.tools import MAX_RESULT, TOOLS, dispatch, specs, tool
from screener.bot.checks import NotPermitted
from screener.bot.commands import COMMANDS, ping
from screener.bot.config import BotConfig

TOKEN = "a-bot-token-that-must-not-be-printed"


# -- config ---------------------------------------------------------------


def test_the_bot_is_inert_without_a_token(monkeypatch):
    # Every other process imports this package indirectly; an unset token must
    # mean "do not start", never "start half-configured".
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert BotConfig.from_env().enabled is False


def test_the_bot_token_is_not_printed():
    # A frozen dataclass repr appears in every traceback that carries the
    # object, and tracebacks reach log aggregators.
    assert TOKEN not in repr(BotConfig(token=TOKEN))


def test_the_allow_list_is_parsed_as_ids_not_names(monkeypatch):
    monkeypatch.setenv("ALLOWED_DISCORD_USER_IDS", " 123 , 456 ")
    config = BotConfig.from_env()
    assert config.allowed_user_ids == frozenset({123, 456})
    assert config.permits(123) and config.permits(456)


def test_a_non_numeric_entry_in_the_allow_list_raises(monkeypatch):
    # The quiet alternative is an allow-list one entry shorter than its author
    # believes, refusing someone for a reason nothing reports.
    monkeypatch.setenv("ALLOWED_DISCORD_USER_IDS", "123,ehewes")
    with pytest.raises(RuntimeError, match="not a Discord user id"):
        BotConfig.from_env()


def test_an_empty_allow_list_permits_nobody(monkeypatch):
    # The same rule as ALLOWED_GITHUB_LOGINS. Reading unset as "everybody"
    # turns a forgotten variable into a bot anyone in the server can drive.
    monkeypatch.delenv("ALLOWED_DISCORD_USER_IDS", raising=False)
    assert BotConfig.from_env().permits(123) is False


def test_a_missing_guild_id_is_none_rather_than_a_crash(monkeypatch):
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    assert BotConfig.from_env().guild_id is None


# -- the command set ------------------------------------------------------


def test_every_command_is_registered_once_with_a_description():
    # COMMANDS is what the client hands to the tree, so a duplicate here would
    # be a duplicate registration against Discord.
    names = [c.name for c in COMMANDS]
    assert names == sorted(set(names))
    assert all(c.description for c in COMMANDS)


def test_ping_is_in_the_command_set():
    assert "ping" in {c.name for c in COMMANDS}


# -- /ping ----------------------------------------------------------------


def body_of(
    command: app_commands.Command[Any, ..., Any],
) -> Callable[[Any], Coroutine[Any, Any, None]]:
    """A command's callback, typed as what it actually is at runtime.

    `Command.callback` is declared to take the binding argument a Cog method
    would receive first. These commands are module-level functions, so the
    runtime callback takes the interaction alone and the declared type is one
    argument wider than the truth.
    """
    return cast(Callable[[Any], Coroutine[Any, Any, None]], command.callback)


class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        self.sent.append((content, ephemeral))

    def is_done(self) -> bool:
        return bool(self.sent)


def fake_interaction(user_id: int = 1):
    """The smallest object /ping actually touches.

    Building a real `discord.Interaction` needs a client and a gateway payload.
    The callback only uses `.user.id`, `.response` and
    `.edit_original_response`, so this is the whole surface under test.
    """
    edits: list[str] = []

    async def edit_original_response(content: str) -> None:
        edits.append(content)

    return types.SimpleNamespace(
        user=types.SimpleNamespace(id=user_id),
        response=FakeResponse(),
        edit_original_response=edit_original_response,
        edits=edits,
    )


def test_ping_reports_the_running_build(monkeypatch):
    # The point of /ping is answering "is the bot I am talking to the build I
    # just deployed", so the SHA is the part that matters, not the word pong.
    monkeypatch.setattr("screener.bot.commands.git_sha", lambda: "abcdef1234567890")
    interaction = fake_interaction()

    asyncio.run(body_of(ping)(interaction))

    assert "abcdef123456" in interaction.response.sent[0][0]
    assert "abcdef123456" in interaction.edits[-1]
    assert "ms" in interaction.edits[-1]


def test_ping_replies_only_to_the_person_who_ran_it(monkeypatch):
    # Ephemeral: a status reply is for the asker, not the channel.
    monkeypatch.setattr("screener.bot.commands.git_sha", lambda: "abcdef1234567890")
    interaction = fake_interaction()
    asyncio.run(body_of(ping)(interaction))
    assert interaction.response.sent[0][1] is True


# -- the authorization gate ------------------------------------------------


def _run_checks(command: app_commands.Command, interaction) -> bool:
    """Run a command's checks the way the tree does, without a tree."""
    for predicate in command.checks:
        result = predicate(interaction)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        if not result:
            return False
    return True


def test_a_user_outside_the_allow_list_is_refused(monkeypatch):
    monkeypatch.setenv("ALLOWED_DISCORD_USER_IDS", "999")
    with pytest.raises(NotPermitted):
        _run_checks(ping, fake_interaction(user_id=123))


def test_a_permitted_user_passes_the_gate(monkeypatch):
    monkeypatch.setenv("ALLOWED_DISCORD_USER_IDS", "123")
    assert _run_checks(ping, fake_interaction(user_id=123)) is True


def test_the_gate_is_read_fresh_so_a_rotated_allow_list_takes_effect(monkeypatch):
    # Secrets land in os.environ at startup, so a value captured at import time
    # would be whatever existed before that.
    monkeypatch.setenv("ALLOWED_DISCORD_USER_IDS", "123")
    assert _run_checks(ping, fake_interaction(user_id=123)) is True
    monkeypatch.setenv("ALLOWED_DISCORD_USER_IDS", "456")
    with pytest.raises(NotPermitted):
        _run_checks(ping, fake_interaction(user_id=123))


# -- the tool registry ----------------------------------------------------


def test_a_tool_spec_is_derived_from_the_signature():
    # Written by hand, a schema goes stale the moment an argument is renamed
    # and tells the model to send something the function no longer takes.
    @tool("demo_echo", "Echo a word back.")
    def demo_echo(word: str, times: int = 1) -> str:
        return word * times

    try:
        spec = next(s for s in specs() if s["function"]["name"] == "demo_echo")
        params = spec["function"]["parameters"]
        assert params["properties"] == {"word": {"type": "string"}, "times": {"type": "integer"}}
        # Only the argument without a default is required.
        assert params["required"] == ["word"]
    finally:
        TOOLS.pop("demo_echo", None)


def test_registering_the_same_tool_twice_is_an_error():
    # A silent overwrite would leave the model calling one implementation while
    # someone reads another.
    @tool("demo_dupe", "First.")
    def first() -> str:
        return "first"

    try:
        with pytest.raises(RuntimeError, match="already registered"):
            tool("demo_dupe", "Second.")(lambda: "second")
    finally:
        TOOLS.pop("demo_dupe", None)


def test_an_unknown_tool_is_reported_rather_than_raised():
    # The model can recover from being told; it cannot recover from an
    # exception that ends the conversation.
    assert "no tool named" in dispatch("does_not_exist", {})


def test_a_failing_tool_becomes_a_message_not_a_crash():
    @tool("demo_boom", "Always fails.")
    def boom() -> str:
        raise ValueError("nope")

    try:
        assert dispatch("demo_boom", {}) == "error: demo_boom failed: ValueError"
    finally:
        TOOLS.pop("demo_boom", None)


def test_bad_arguments_are_reported_so_the_model_can_correct_itself():
    @tool("demo_args", "Needs a word.")
    def needs(word: str) -> str:
        return word

    try:
        assert "bad arguments" in dispatch("demo_args", {"wrong": "x"})
    finally:
        TOOLS.pop("demo_args", None)


def test_a_long_tool_result_is_truncated():
    # A tool result is context on the next request, so a chatty one is paid for
    # twice: once to receive and once to send back.
    @tool("demo_long", "Returns a lot.")
    def long() -> str:
        return "x" * 5000

    try:
        assert len(dispatch("demo_long", {})) <= MAX_RESULT + 1
    finally:
        TOOLS.pop("demo_long", None)


def test_the_status_tool_reports_compactly():
    # key=value rather than JSON: the model reads it just as well and it costs
    # roughly a third of the tokens, on every message that asks.
    result = dispatch("status", {})
    assert result.startswith("build=")
    assert "migrations=" in result


# -- Steven ----------------------------------------------------------------


def fake_completion(text="", tool_calls=(), tokens=10, cost=0.0):
    return types.SimpleNamespace(
        text=text, model="upstage/solar-pro4",
        prompt_tokens=tokens, completion_tokens=tokens, cost_usd=cost,
        finish_reason="stop", tool_calls=tuple(tool_calls),
        raw_message={"role": "assistant", "content": text},
    )


def test_steven_answers_without_tools_when_none_are_needed(monkeypatch):
    monkeypatch.setattr(agent, "converse", lambda **kw: fake_completion(text="Percentiles are sector-relative."))
    assert asyncio.run(agent.answer("how does scoring work")) == "Percentiles are sector-relative."


def test_steven_calls_a_tool_then_answers_with_its_result(monkeypatch):
    calls: list[dict] = []
    replies = [
        fake_completion(tool_calls=[types.SimpleNamespace(id="c1", name="status", arguments={})]),
        fake_completion(text="build=abc, database ok."),
    ]

    def converse(**kw):
        calls.append(kw)
        return replies.pop(0)

    monkeypatch.setattr(agent, "converse", converse)
    monkeypatch.setattr(agent, "dispatch", lambda name, args: "build=abc db=ok migrations=10")

    assert asyncio.run(agent.answer("is the deployment healthy")) == "build=abc, database ok."
    # The tool result must be fed back as a `tool` message, and the assistant
    # turn echoed verbatim, or the provider rejects the follow-up.
    second_turn = calls[1]["messages"]
    assert second_turn[-1]["role"] == "tool"
    assert second_turn[-1]["content"] == "build=abc db=ok migrations=10"
    assert second_turn[-2]["role"] == "assistant"


def test_steven_is_offered_the_tools_on_every_request(monkeypatch):
    seen: list = []
    monkeypatch.setattr(agent, "converse", lambda **kw: seen.append(kw["tools"]) or fake_completion(text="ok"))
    asyncio.run(agent.answer("hello"))
    assert any(s["function"]["name"] == "status" for s in seen[0])


def test_a_tool_loop_that_never_settles_stops_rather_than_billing_forever(monkeypatch):
    # Without a cap, a model that keeps asking for tools re-sends the whole
    # conversation each round and the cost grows with no answer at the end.
    forever = types.SimpleNamespace(id="c", name="status", arguments={})
    monkeypatch.setattr(agent, "converse", lambda **kw: fake_completion(tool_calls=[forever]))
    monkeypatch.setattr(agent, "dispatch", lambda name, args: "build=abc")
    assert "could not finish" in asyncio.run(agent.answer("loop please"))


def test_conversations_carry_no_history_between_mentions(monkeypatch):
    # The largest token saving available, and honest: Steven has no memory, and
    # silently accumulating context gets more expensive every message.
    seen: list[int] = []
    monkeypatch.setattr(
        agent, "converse",
        lambda **kw: seen.append(len(kw["messages"])) or fake_completion(text="ok"),
    )
    asyncio.run(agent.answer("first"))
    asyncio.run(agent.answer("second"))
    assert seen == [2, 2]  # system + user, every time


def test_the_agent_answers_on_solar_by_default(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_MODEL", raising=False)
    assert agent.agent_model() == "upstage/solar-pro4"
    assert agent.agent_model() in MODELS


def test_an_unknown_override_still_bills_against_a_known_model(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_MODEL", "upstage/solar-pro4-typo")
    assert resolve_model(agent.agent_model()) in MODELS


def test_a_reply_longer_than_discord_allows_is_cut_rather_than_dropped(monkeypatch):
    monkeypatch.setattr(agent, "converse", lambda **kw: fake_completion(text="word " * 900))
    reply = asyncio.run(agent.answer("go on then"))
    assert len(reply) <= agent.MAX_REPLY
    assert reply.endswith("(truncated)")


def test_a_model_failure_becomes_a_sentence_not_a_traceback(monkeypatch):
    def boom(**kw):
        raise AiError("upstream is down")

    monkeypatch.setattr(agent, "converse", boom)
    assert "could not reach the model" in asyncio.run(agent.answer("hello"))


def test_an_empty_mention_says_so_rather_than_asking_the_model(monkeypatch):
    def never(**kw):
        raise AssertionError("the model must not be called for an empty question")

    monkeypatch.setattr(agent, "converse", never)
    assert "no text" in asyncio.run(agent.answer("   "))


def test_the_presence_carries_the_build_and_model(monkeypatch):
    # This used to be appended to every reply, which repeated the same line
    # under every answer. As a status it is said once and always visible.
    monkeypatch.setattr(agent, "git_sha", lambda: "abcdef1234567890")
    status = agent.presence()
    assert "abcdef1" in status and "solar-pro4" in status
    # Discord truncates a custom status around 128 characters.
    assert len(status) < 128


def test_steven_is_told_he_may_describe_his_own_tools():
    # "what do you have access to" is the obvious first question, and the tool
    # specs are already in context, so this costs one sentence rather than a
    # tool that exists to describe tools.
    assert "have access to" in agent.SYSTEM_PROMPT


def test_the_system_prompt_forbids_advice_and_invented_numbers():
    # The two hard constraints in DESIGN.md. If this loosens, the bot becomes
    # the one part of the system that makes buy calls.
    prompt = agent.SYSTEM_PROMPT.lower()
    assert "no investment advice" in prompt
    assert "never invent a number" in prompt


def test_the_prompt_stays_small_enough_to_send_on_every_message():
    # Steven is mentioned casually, so the fixed overhead is paid constantly.
    # This is a budget, not a style rule: if the prompt grows past it, that is
    # a decision worth making on purpose.
    import json

    # Raised from 1200 when the chart tool landed: a second tool, its
    # enumerated `mark` argument, and two lines of prompt telling Steven when to
    # reach for it. That is roughly 600 characters and it bought a feature.
    # Left as tight as the old one — about 40 characters of headroom — so the
    # next thing that grows the prompt is also a decision and not a drift.
    overhead = len(agent.SYSTEM_PROMPT) + len(json.dumps(specs(), separators=(",", ":")))
    assert overhead < 1800


# -- the Discord handoff ---------------------------------------------------


def test_the_user_map_is_parsed_and_case_folded(monkeypatch):
    # GitHub logins are case-insensitive and a session carries whatever casing
    # the owner chose, so the lookup has to fold both sides.
    monkeypatch.setenv("DISCORD_USER_MAP", "ehewes:280752191059263488, D1K03:401071550331355146")
    config = BotConfig.from_env()
    assert config.discord_id_for("EHEWES") == 280752191059263488
    assert config.discord_id_for("d1k03") == 401071550331355146


def test_an_unmapped_login_has_no_discord_account(monkeypatch):
    monkeypatch.setenv("DISCORD_USER_MAP", "ehewes:280752191059263488")
    assert BotConfig.from_env().discord_id_for("someone-else") is None


def test_a_malformed_user_map_entry_raises(monkeypatch):
    # Dropping it silently would mean one person's handoff button quietly does
    # nothing, reported as "it doesn't work".
    monkeypatch.setenv("DISCORD_USER_MAP", "ehewes:280752191059263488,daniel")
    with pytest.raises(RuntimeError, match="login:discord_id"):
        BotConfig.from_env()


def test_no_discord_account_ids_are_written_into_the_repository():
    # The map lives in Infisical. This is the test that notices if someone
    # pastes an id into a config file to make something work.
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    snowflake = re.compile(r"\b\d{17,20}\b")
    checked = 0
    for path in [*root.glob("src/**/*.py"), *(root / "deploy").glob("*.y*ml"), root / ".env.example"]:
        if not path.is_file():
            continue
        checked += 1
        found = snowflake.findall(path.read_text())
        assert not found, f"{path.name} contains what looks like a Discord id: {found}"
    assert checked > 5


def test_a_handoff_without_a_mapped_account_says_so(monkeypatch):
    from screener.bot.handoff import HandoffError, send_dm

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "a-token")
    monkeypatch.setenv("DISCORD_USER_MAP", "")
    with pytest.raises(HandoffError, match="no Discord account is mapped"):
        send_dm(login="nobody", text="hello")


def test_a_handoff_opens_a_dm_then_posts_to_it(monkeypatch):
    # Discord needs a DM channel to exist before anything can be posted to it,
    # so this is two calls and the order matters.
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "a-token")
    monkeypatch.setenv("DISCORD_USER_MAP", "ehewes:280752191059263488")
    from screener.bot.handoff import send_dm

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/channels") and "users" in request.url.path:
            return httpx.Response(200, json={"id": "chan-1"})
        return httpx.Response(200, json={"id": "msg-1"})

    assert send_dm(login="ehewes", text="hi", transport=httpx.MockTransport(handler)) == 280752191059263488
    assert seen[0].endswith("/users/@me/channels")
    assert seen[1].endswith("/channels/chan-1/messages")


def test_direct_messages_being_closed_is_reported_plainly(monkeypatch):
    # A 403 here is almost always "DMs from server members are off", which is
    # a thing the person can fix if they are told.
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "a-token")
    monkeypatch.setenv("DISCORD_USER_MAP", "ehewes:280752191059263488")
    from screener.bot.handoff import HandoffError, send_dm

    def handler(request: httpx.Request) -> httpx.Response:
        if "users" in request.url.path:
            return httpx.Response(200, json={"id": "chan-1"})
        return httpx.Response(403, json={})

    with pytest.raises(HandoffError, match="switched off"):
        send_dm(login="ehewes", text="hi", transport=httpx.MockTransport(handler))
