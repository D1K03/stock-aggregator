import asyncio
import types
from collections.abc import Callable, Coroutine
from typing import Any, cast

import httpx
import pytest
from discord import app_commands

from screener import audit
from screener.ai import AiError
from screener.ai.models import MODELS, resolve_model
from screener.bot import agent, client
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


def test_the_conversation_so_far_is_re_sent_so_a_follow_up_has_an_antecedent(monkeypatch):
    # Without this, "chart it" is answered with "which ticker?" — the exchange
    # that named the ticker is the whole reason the second message parses.
    seen: list[list[dict]] = []
    monkeypatch.setattr(
        agent, "converse",
        lambda **kw: seen.append(kw["messages"]) or fake_completion(text="ok"),
    )
    monkeypatch.setattr(
        agent, "_recall", lambda actor, kind: [("how is NVDA doing", "82, top quartile.")]
    )
    asyncio.run(agent.answer("chart it", actor="2807"))
    assert [m["role"] for m in seen[0]] == ["system", "user", "assistant", "user"]
    assert seen[0][-1]["content"] == "chart it"


def test_only_the_last_couple_of_exchanges_are_remembered(monkeypatch):
    # The memory is the one part of this prompt that grows with use, and a
    # remembered turn is re-sent on every round of every message after it. Two
    # exchanges is the cap; the query, not the model, enforces it.
    assert audit.MEMORY_EXCHANGES == 2

    seen: list[list[dict]] = []
    monkeypatch.setattr(
        agent, "converse",
        lambda **kw: seen.append(kw["messages"]) or fake_completion(text="ok"),
    )
    monkeypatch.setattr(agent, "_recall", lambda actor, kind: [("a", "b"), ("c", "d")])
    asyncio.run(agent.answer("and now", actor="2807"))
    assert len(seen[0]) == 1 + 2 * audit.MEMORY_EXCHANGES + 1


def test_a_new_conversation_does_not_inherit_the_last_one(monkeypatch):
    # New chat on the dashboard has to clear what Steven remembers as well as
    # what is on screen, or the button is a lie and the next thread is billed
    # for the previous one.
    def never(actor, kind):
        raise AssertionError("a fresh conversation must not recall anything")

    monkeypatch.setattr(agent, "_recall", never)
    monkeypatch.setattr(agent, "converse", lambda **kw: fake_completion(text="ok"))
    reply = asyncio.run(
        agent.respond("hello", actor="ehewes", actor_kind="github", fresh=True)
    )
    assert reply.text == "ok"


def test_what_is_remembered_is_truncated_before_it_is_stored(monkeypatch):
    # Stored short rather than trimmed at recall, because one long answer would
    # otherwise be paid for on every round of every message that remembers it.
    recorded: list[dict] = []
    monkeypatch.setattr(agent, "converse", lambda **kw: fake_completion(text="word " * 900))
    monkeypatch.setattr(agent, "_recall", lambda actor, kind: [])
    monkeypatch.setattr(agent, "record", lambda **kw: recorded.append(kw))
    asyncio.run(agent.respond("go on then " * 90, actor="2807", actor_kind="discord"))
    detail = recorded[0]["detail"]
    assert len(detail["reply"]) == agent.MEMORY_CHARS
    assert len(detail["question"]) == agent.MEMORY_CHARS


def test_a_reply_nobody_asked_for_is_not_remembered(monkeypatch):
    # Scheduled work is not a conversation, and recalling one would put the
    # platform's own questions in front of the next person who asks something.
    def never(actor, kind):
        raise AssertionError("system work has no conversation to recall")

    monkeypatch.setattr(agent, "_recall", never)
    monkeypatch.setattr(agent, "converse", lambda **kw: fake_completion(text="ok"))
    assert asyncio.run(agent.respond("nightly check")).text == "ok"


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

    # Raised again from 1800 when the `sql` tool landed. Roughly 400 characters,
    # and what it buys is the only tool here that returns a real figure from real
    # data: `chart` draws invented concept numbers and `status` reports process
    # facts. Left as tight as the last two.
    #
    # Raised from 1200 when the chart tool landed: a second tool, its
    # enumerated `mark` argument, and two lines of prompt telling Steven when to
    # reach for it. That is roughly 600 characters and it bought a feature.
    # Left as tight as the old one — about 40 characters of headroom — so the
    # next thing that grows the prompt is also a decision and not a drift.
    overhead = len(agent.SYSTEM_PROMPT) + len(json.dumps(specs(), separators=(",", ":")))
    assert overhead < 2200


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


# -- who the bot answers ---------------------------------------------------


def test_a_mention_is_needed_in_a_channel():
    # Anywhere other people are talking, replying to everything is noise.
    assert client.wants_reply(direct=False, mentioned=True) is True
    assert client.wants_reply(direct=False, mentioned=False) is False


def test_a_direct_message_needs_no_mention():
    # There is nobody else it could be for. Making someone @-mention the only
    # other participant in a two-person conversation reads as broken, and the
    # handoff sends people straight into exactly that DM.
    assert client.wants_reply(direct=True, mentioned=False) is True


# -- what the bot hears -----------------------------------------------------


def test_a_message_with_no_voice_attachment_is_answered_the_way_it_always_was():
    assert client.hearing([]) == "nothing"


def test_a_voice_message_short_enough_to_hear_is_picked_up():
    assert client.hearing([4.2]) == "transcribe"


def test_a_voice_message_over_the_cap_is_declined_rather_than_transcribed():
    # Declined on the duration Discord already told us, so the decision is made
    # before anything is downloaded and before a core is spent.
    assert client.hearing([client.MAX_SECONDS + 1]) == "too_long"


def test_a_voice_message_of_unknown_length_is_declined_rather_than_guessed():
    # Discord sets duration and waveform together, so this should not happen —
    # and if it does, refusing is cheaper than decoding something unbounded.
    assert client.hearing([None]) == "too_long"
    assert client.hearing([0.0]) == "too_long"


def test_the_first_voice_message_wins_when_someone_sends_two():
    assert client.hearing([3.0, client.MAX_SECONDS + 1]) == "transcribe"


def test_a_transcript_is_quoted_above_the_answer():
    out = client.with_transcript("what did nvidia do", "It rose 8%.")
    assert out.startswith(client.QUOTE)
    assert "what did nvidia do" in out
    assert out.endswith("It rose 8%.")


def test_a_quote_gives_way_to_the_answer_when_both_will_not_fit():
    # Discord rejects anything over 2000 characters outright, and the answer is
    # the thing that was asked for. Losing the receipt beats losing the reply.
    answer = "x" * agent.MAX_REPLY
    assert client.with_transcript("something said out loud", answer) == answer


def test_a_long_transcript_is_cut_rather_than_the_answer():
    out = client.with_transcript("word " * 200, "Short answer.")
    assert out.endswith("Short answer.")
    assert len(out.splitlines()[0]) <= client.MAX_QUOTE + len(client.QUOTE) + 10


def test_a_transcript_of_only_whitespace_leaves_the_answer_alone():
    assert client.with_transcript("   ", "Answer.") == "Answer."


def test_an_allowance_the_caller_already_checked_is_not_checked_again(monkeypatch):
    # The voice path has to check before it spends a core on transcription, and
    # checking again here would be two connections and two sums for one turn.
    from decimal import Decimal

    from screener.bot import budget

    def explode(*args, **kwargs):
        raise AssertionError("the cap was checked twice for one turn")

    monkeypatch.setattr(budget, "check", explode)
    monkeypatch.setattr(agent, "converse", lambda **kw: fake_completion(text="Fine."))
    monkeypatch.setattr(agent, "record", lambda **kw: None)

    allowance = budget.Budget(spent=Decimal("0"), cap=Decimal("1"), allowed=True)
    reply = asyncio.run(agent.respond("hi", allowance=allowance, actor="1", actor_kind="discord"))
    assert reply.text == "Fine."


def test_an_allowance_the_caller_already_refused_produces_the_same_refusal(monkeypatch):
    # Refused before transcription, so the question arrives empty — and the
    # refusal sentence still has to come from the one place it lives, not from
    # the empty-question guard below it.
    from decimal import Decimal

    from screener.bot import budget

    monkeypatch.setattr(agent, "record", lambda **kw: None)
    monkeypatch.setattr(agent, "converse", lambda **kw: fake_completion(text="unreachable"))

    refused = budget.Budget(spent=Decimal("1"), cap=Decimal("1"), allowed=False)
    reply = asyncio.run(agent.respond("", allowance=refused, actor="1", actor_kind="discord"))
    assert "daily spend cap" in reply.text


def test_a_spoken_turn_is_marked_as_one_in_the_audit_detail(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(agent, "converse", lambda **kw: fake_completion(text="Sure."))
    monkeypatch.setattr(agent, "record", lambda **kw: rows.append(kw))
    monkeypatch.setattr(
        "screener.bot.budget.check",
        lambda a, k: __import__("screener.bot.budget", fromlist=["Budget"]).Budget(
            spent=__import__("decimal").Decimal("0"),
            cap=__import__("decimal").Decimal("1"),
            allowed=True,
        ),
    )

    asyncio.run(agent.respond("chart nvidia", voice=True, actor="1", actor_kind="discord"))
    assert rows and rows[-1]["detail"]["voice"] is True
    # The question is in the row, because that is where Steven's memory lives
    # and a spoken question is a question. The flag is what tells it from a
    # typed one when reading the trail back, which is the whole point of it.
    assert rows[-1]["detail"]["question"] == "chart nvidia"


# -- the sql tool -----------------------------------------------------------


def test_the_sql_tool_says_the_console_is_off_rather_than_failing(monkeypatch):
    monkeypatch.delenv("PLAYGROUND_DATABASE_URL", raising=False)
    assert "not configured" in dispatch("sql", {"query": "select 1"})


def test_a_small_result_is_said_rather_than_shown(monkeypatch):
    # A chart is never a scalar; a query often is. "How many securities are
    # there" wants the number in the sentence, not a one-cell table.
    from screener import playground
    from screener.bot.tools import query as tool_module

    monkeypatch.setattr(playground, "enabled", lambda: True)
    monkeypatch.setattr(tool_module.playground, "enabled", lambda: True)
    monkeypatch.setattr(
        tool_module.playground,
        "run",
        lambda q, n: playground.Result(
            columns=(playground.Column("n", "bigint"),),
            rows=((412,),), row_count=1, truncated=False, shortened=0, ms=3, limit=n,
        ),
    )
    assert dispatch("sql", {"query": "select count(*) as n from security"}) == "n=412"


def test_a_wide_result_reaches_the_surface_rather_than_the_model(monkeypatch):
    from screener import playground
    from screener.bot.tools import collecting_rows
    from screener.bot.tools import query as tool_module

    monkeypatch.setattr(tool_module.playground, "enabled", lambda: True)
    monkeypatch.setattr(
        tool_module.playground,
        "run",
        lambda q, n: playground.Result(
            columns=(playground.Column("sym", "text"), playground.Column("v", "numeric")),
            rows=tuple((f"S{i}", str(i)) for i in range(20)),
            row_count=20, truncated=False, shortened=0, ms=7, limit=n,
        ),
    )
    with collecting_rows(True) as selected:
        said = dispatch("sql", {"query": "select sym, v from t"})
    # The model is told the shape; the rows travelled beside the reply.
    assert "rows=20" in said and "S0" not in said
    assert len(selected) == 1 and len(selected[0].rows) == 20


def test_discord_gets_the_rows_as_text_because_it_cannot_render_a_table(monkeypatch):
    from screener import playground
    from screener.bot.tools import collecting_rows
    from screener.bot.tools import query as tool_module

    monkeypatch.setattr(tool_module.playground, "enabled", lambda: True)
    monkeypatch.setattr(
        tool_module.playground,
        "run",
        lambda q, n: playground.Result(
            columns=(playground.Column("sym", "text"), playground.Column("v", "numeric")),
            rows=tuple((f"S{i}", str(i)) for i in range(20)),
            row_count=20, truncated=False, shortened=0, ms=7, limit=n,
        ),
    )
    with collecting_rows(False) as selected:
        said = dispatch("sql", {"query": "select sym, v from t"})
    assert "S0" in said and selected == []


def test_an_unknown_table_comes_back_with_the_list(monkeypatch):
    # Naming the set on the round the model got it wrong costs nothing per
    # message, which a second `tables` tool would.
    from screener import playground
    from screener.bot.tools import query as tool_module

    def boom(q, n):
        raise playground.QueryError(message='relation "nope" does not exist', sqlstate="42P01")

    monkeypatch.setattr(tool_module.playground, "enabled", lambda: True)
    monkeypatch.setattr(tool_module.playground, "run", boom)
    monkeypatch.setattr(tool_module, "_known_tables", lambda: "public.security public.metric")
    said = dispatch("sql", {"query": "select * from nope"})
    assert "does not exist" in said and "public.security" in said
