"""Steven's control over a capture.

Control only, and one of these tests is about what he *cannot* do: there is no
tool here that returns transcript text, and a line count is not the transcript.
"""

from datetime import UTC, datetime

import pytest

from screener.bot.tools import MAX_RESULT, TOOLS, acting, dispatch, specs
from screener.bot.tools import skybird as tools
from screener.skybird import store

LIVE = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
TWITCH = "https://www.twitch.tv/somestreamer"


@pytest.fixture
def wired(monkeypatch, db_url, fresh_db):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.delenv("SKYBIRD_MAX_SESSIONS", raising=False)
    monkeypatch.delenv("SKYBIRD_CHUNK_SECONDS", raising=False)
    return fresh_db


# -- registration -----------------------------------------------------------


def test_the_three_tools_are_offered_to_the_model():
    offered = {spec["function"]["name"] for spec in specs()}
    assert {"watch", "captures", "hold"} <= offered


def test_the_descriptions_stay_short_enough_to_send_every_message():
    # Each of these is re-sent on every request of every conversation, so a
    # sentence here is a sentence paid for hundreds of times.
    for name in ("watch", "captures", "hold"):
        assert len(TOOLS[name].description) < 100


def test_hold_takes_an_id_and_an_action():
    schema = TOOLS["hold"].parameters
    assert schema["properties"]["session"]["type"] == "integer"
    assert set(schema["required"]) == {"session", "action"}


# -- starting ---------------------------------------------------------------


def test_watch_starts_a_capture_and_says_where_it_got_to(wired):
    # 'requested' rather than 'running': the supervisor has not seen it yet, and
    # claiming otherwise would be a state Steven made up.
    result = tools.watch(LIVE)
    assert result.startswith("#1 requested youtube")
    assert "(1/2)" in result
    assert store.get(wired, 1) is not None


def test_a_capture_records_who_asked_rather_than_who_started_it(wired):
    # `requested_by` is shown in the dashboard beside the capture. "steven"
    # would be true of every row Steven made and would tell nobody anything.
    with acting("ehewes", "github"):
        tools.watch(LIVE)
    session = store.get(wired, 1)
    assert session is not None and session.requested_by == "ehewes"


def test_outside_a_reply_a_capture_belongs_to_nobody_in_particular(wired):
    tools.watch(LIVE)
    session = store.get(wired, 1)
    assert session is not None and session.requested_by == "system"


def test_a_link_from_a_platform_we_do_not_have_comes_back_as_a_sentence(wired):
    # A sentence rather than an exception, so the model can relay it and carry
    # on instead of the conversation ending on a stack trace.
    result = tools.watch("https://vimeo.com/12345")
    assert "YouTube" in result and "Twitch" in result


def test_the_same_stream_twice_is_refused(wired):
    tools.watch(LIVE)
    assert tools.watch("https://youtu.be/jNQXAC9IVRw") == (
        "that stream is already being captured"
    )


def test_the_limit_is_named_in_the_refusal_and_says_what_to_do(wired, monkeypatch):
    """What stops Steven promising a stream he cannot have.

    Refused in the tool as well as at the API, so the model learns the limit in
    the same breath as being stopped by it — otherwise it tries again.
    """
    monkeypatch.setenv("SKYBIRD_MAX_SESSIONS", "1")
    tools.watch(LIVE)
    result = tools.watch(TWITCH)
    assert "1/1" in result
    assert "pause or stop one first" in result


def test_a_paused_capture_frees_the_slot(wired, monkeypatch):
    monkeypatch.setenv("SKYBIRD_MAX_SESSIONS", "1")
    tools.watch(LIVE)
    tools.hold(1, "pause")
    assert tools.watch(TWITCH).startswith("#2 requested twitch")


# -- listing ----------------------------------------------------------------


def test_captures_reports_the_limit_even_with_nothing_running(wired):
    # This is where the model actually learns the cap: SKYBIRD_MAX_SESSIONS is
    # configuration, and the system prompt is built once at import, before
    # secrets are loaded, so a number in it would be the default frozen in.
    assert tools.captures() == "0/2 capturing, nothing to show"


def test_captures_lists_the_id_and_state_of_each(wired):
    tools.watch(LIVE)
    tools.watch(TWITCH)
    result = tools.captures()
    assert result.startswith("2/2 capturing")
    assert "#1 requested" in result and "#2 requested" in result


def test_a_paused_capture_is_listed_but_not_counted(wired):
    tools.watch(LIVE)
    tools.hold(1, "pause")
    result = tools.captures()
    assert result.startswith("0/2 capturing")
    assert "#1 paused" in result


def test_steven_is_never_handed_the_transcript(wired):
    """The constraint this module exists inside.

    He starts and stops captures; reading them back is somebody else's feature.
    A line count is not the transcript — it is how a working capture is told
    from one that is quietly failing.
    """
    tools.watch(LIVE)
    store.append_segments(
        wired,
        1,
        [(1, 0, datetime.now(UTC), 0.0, 2.0, "a distinctive phrase nobody should see")],
    )
    result = tools.captures()
    assert "distinctive" not in result
    assert "1L" in result


def test_a_long_title_is_cut_rather_than_sent_whole(wired):
    tools.watch(LIVE)
    wired.execute(
        "update skybird.stream_session set title = %s where id = 1",
        ("a" * 300,),
    )
    result = tools.captures()
    assert "…" in result
    assert len(result) < MAX_RESULT


# -- holding ----------------------------------------------------------------


@pytest.mark.parametrize(
    "action,state",
    [("pause", "paused"), ("stop", "stopped")],
)
def test_hold_moves_a_capture(wired, action, state):
    tools.watch(LIVE)
    assert tools.hold(1, action) == f"#1 {state}"


def test_resume_puts_a_held_capture_back_in_the_queue(wired):
    tools.watch(LIVE)
    tools.hold(1, "pause")
    assert tools.hold(1, "resume") == "#1 requested"


def test_an_action_the_model_invented_comes_back_correctable(wired):
    tools.watch(LIVE)
    assert tools.hold(1, "halt") == "action must be one of pause, resume, stop"


def test_the_action_is_read_leniently(wired):
    # A model that sends " Pause " has not made a mistake worth a refusal.
    tools.watch(LIVE)
    assert tools.hold(1, " Pause ") == "#1 paused"


def test_holding_a_capture_that_is_not_there(wired):
    assert tools.hold(99, "pause") == "no capture #99"


def test_a_transition_that_does_not_apply_says_what_state_it_is_in(wired):
    tools.watch(LIVE)
    tools.hold(1, "stop")
    assert tools.hold(1, "resume") == "#1 is stopped, cannot resume it"


# -- through the dispatcher -------------------------------------------------


def test_the_dispatcher_runs_them_and_records_who_asked(wired):
    with acting("ehewes", "github"):
        result = dispatch("watch", {"url": LIVE})
    assert result.startswith("#1 requested")

    with wired.cursor() as cur:
        cur.execute(
            "select actor, actor_kind from audit.event "
            "where kind = 'tool' and operation = 'watch'"
        )
        row = cur.fetchone()
    assert row == ("ehewes", "github")


def test_bad_arguments_come_back_as_text_rather_than_ending_the_conversation(wired):
    assert dispatch("hold", {"session": 1}).startswith("error: bad arguments")


def test_a_stray_reference_is_not_left_behind_by_a_reply(wired):
    with acting("ehewes", "github"):
        pass
    tools.watch(LIVE)
    session = store.get(wired, 1)
    assert session is not None and session.requested_by == "system"
