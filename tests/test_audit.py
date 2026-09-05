import json
from decimal import Decimal

from screener import audit


def insert(conn, **overrides):
    """One audit row, straight in, so a test can shape the trail it needs."""
    values = {
        "kind": "agent",
        "operation": "steven.reply",
        "actor": "42",
        "actor_kind": "discord",
        "outcome": "ok",
        "model": "upstage/solar-pro4",
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cost_usd": Decimal("0.00003"),
        "duration_ms": 900,
        "detail": {},
    }
    values.update(overrides)
    values["detail"] = json.dumps(values["detail"])
    conn.execute(
        """
        insert into audit.event (
            kind, operation, actor, actor_kind, outcome, model,
            prompt_tokens, completion_tokens, cost_usd, duration_ms, detail
        ) values (
            %(kind)s, %(operation)s, %(actor)s, %(actor_kind)s, %(outcome)s,
            %(model)s, %(prompt_tokens)s, %(completion_tokens)s, %(cost_usd)s,
            %(duration_ms)s, %(detail)s
        )
        """,
        values,
    )


# -- paging ----------------------------------------------------------------


def test_a_page_holds_fifty_and_reports_how_many_there_are(fresh_db):
    for _ in range(120):
        insert(fresh_db)

    events, total = audit.page(fresh_db)
    assert len(events) == 50
    # The count comes back alongside so the interface can say "page 1 of 3"
    # rather than discovering the end by walking off it.
    assert total == 120


def test_paging_walks_the_whole_trail_without_repeating_a_row(fresh_db):
    for _ in range(120):
        insert(fresh_db)

    seen: list[int] = []
    for page_number in range(3):
        events, _ = audit.page(fresh_db, offset=page_number * audit.PAGE_SIZE)
        seen.extend(e.id for e in events)

    assert len(seen) == 120
    assert len(set(seen)) == 120


def test_the_newest_event_is_first(fresh_db):
    insert(fresh_db, operation="older")
    insert(fresh_db, operation="newer")
    events, _ = audit.page(fresh_db)
    assert events[0].operation == "newer"


# -- filtering -------------------------------------------------------------


def test_filtering_by_kind_narrows_both_the_rows_and_the_count(fresh_db):
    for _ in range(3):
        insert(fresh_db, kind="agent")
    for _ in range(2):
        insert(fresh_db, kind="command", operation="ping")

    events, total = audit.page(fresh_db, kind="command")
    assert total == 2
    assert {e.kind for e in events} == {"command"}


def test_filtering_by_operation_picks_out_one_agent(fresh_db):
    # The interface offers kind first and the specific operation within it, so
    # "agent" narrows to Steven rather than to every agent there will ever be.
    insert(fresh_db, kind="agent", operation="steven.reply")
    insert(fresh_db, kind="agent", operation="someone.else")

    events, total = audit.page(fresh_db, kind="agent", operation="steven.reply")
    assert total == 1
    assert events[0].operation == "steven.reply"


def test_an_unknown_kind_is_ignored_rather_than_returning_nothing(fresh_db):
    # A filter value outside the enum can only come from a hand-edited URL, and
    # an empty table is a worse answer than an unfiltered one.
    insert(fresh_db)
    _, total = audit.page(fresh_db, kind="not-a-kind")
    assert total == 1


def test_a_filter_value_cannot_smuggle_sql(fresh_db):
    insert(fresh_db)
    # Composed rather than interpolated, so this is a string that matches
    # nothing rather than a statement.
    _, total = audit.page(fresh_db, operation="x'; drop schema audit cascade; --")
    assert total == 0
    # Still there.
    assert audit.page(fresh_db)[1] == 1


# -- spend -----------------------------------------------------------------


def test_spend_totals_what_was_actually_billed(fresh_db):
    insert(fresh_db, cost_usd=Decimal("0.00003"), prompt_tokens=100, completion_tokens=20)
    insert(fresh_db, cost_usd=Decimal("0.00007"), prompt_tokens=200, completion_tokens=30)

    totals = audit.spend(fresh_db)
    assert totals.events == 2
    assert totals.total_cost == Decimal("0.00010000")
    assert totals.total_tokens == 350


def test_spend_separates_the_last_day_from_all_time(fresh_db):
    # A number that only ever grows says nothing about whether today changed.
    insert(fresh_db, cost_usd=Decimal("0.00005"))
    fresh_db.execute(
        "insert into audit.event (kind, operation, cost_usd, occurred_at) "
        "values ('agent', 'old', 0.00002, now() - interval '3 days')"
    )

    totals = audit.spend(fresh_db)
    assert totals.total_cost == Decimal("0.00007000")
    assert totals.cost_24h == Decimal("0.00005000")
    assert totals.events_24h == 1


def test_spend_on_an_empty_trail_is_zero_not_an_error(fresh_db):
    totals = audit.spend(fresh_db)
    assert totals.events == 0
    assert totals.total_cost == 0


# -- the filter options ----------------------------------------------------


def test_the_filter_options_come_from_what_has_actually_happened(fresh_db):
    # Built from the trail rather than a hardcoded list, so a new operation
    # appears the first time it happens without anyone remembering to add it.
    insert(fresh_db, kind="agent", operation="steven.reply")
    insert(fresh_db, kind="agent", operation="steven.reply")
    insert(fresh_db, kind="command", operation="ping")

    assert audit.operations(fresh_db) == [
        ("agent", "steven.reply", 2),
        ("command", "ping", 1),
    ]


# -- writing ---------------------------------------------------------------


def test_recording_never_raises_when_the_database_is_unreachable(monkeypatch):
    # An audit failure must not be the reason an operation fails. The caller is
    # in the middle of answering someone.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    audit.record(kind="agent", operation="steven.reply")  # must not raise


def test_a_recorded_event_reads_back_with_its_cost(fresh_db, monkeypatch, db_url):
    monkeypatch.setenv("DATABASE_URL", db_url)
    audit.record(
        kind="tool",
        operation="status",
        actor="42",
        actor_kind="discord",
        cost_usd=0.000123,
        prompt_tokens=10,
        detail={"arguments": {}},
    )

    events, total = audit.page(fresh_db, kind="tool")
    assert total == 1
    assert events[0].operation == "status"
    assert events[0].cost_usd == Decimal("0.00012300")
    assert events[0].detail == {"arguments": {}}


# -- spend per person ------------------------------------------------------


def test_spend_is_attributed_to_the_person_who_asked(fresh_db):
    # Two people share one bill. A single total says the month was expensive
    # without saying whose questions made it so.
    insert(fresh_db, actor="ehewes", actor_kind="github", cost_usd=Decimal("0.0400"))
    insert(fresh_db, actor="ehewes", actor_kind="github", cost_usd=Decimal("0.0200"))
    insert(fresh_db, actor="401071550331355146", cost_usd=Decimal("0.0100"))

    rows = audit.by_actor(fresh_db)
    assert [(r.actor, float(r.cost)) for r in rows] == [
        ("ehewes", 0.06),
        ("401071550331355146", 0.01),
    ]
    assert rows[0].events == 2
    assert rows[0].actor_kind == "github"


def test_the_same_person_on_two_surfaces_is_two_rows(fresh_db):
    # Joining a GitHub login to a Discord id needs a mapping this layer does
    # not hold, so they are reported separately and each says which surface it
    # is, rather than being silently merged into a number nobody can check.
    insert(fresh_db, actor="ehewes", actor_kind="github", cost_usd=Decimal("0.02"))
    insert(fresh_db, actor="ehewes", actor_kind="discord", cost_usd=Decimal("0.03"))

    rows = audit.by_actor(fresh_db)
    assert len(rows) == 2
    assert {r.actor_kind for r in rows} == {"github", "discord"}


def test_people_who_cost_nothing_are_left_out(fresh_db):
    # The trail records tool calls and slash commands too. A list of people who
    # spent nothing is noise on a panel whose whole subject is money.
    insert(fresh_db, kind="tool", operation="chart", cost_usd=Decimal("0"))
    insert(fresh_db, kind="command", operation="ping", cost_usd=Decimal("0"))
    assert audit.by_actor(fresh_db) == []


def test_work_with_nobody_behind_it_is_not_counted_as_a_person(fresh_db):
    # `actor` is `not null default 'system'`, so scheduled work is not absent
    # from the trail, it is attributed to the machine. A panel headed "spend by
    # person" listing "system" would be wrong in a way that reads as a bug.
    insert(fresh_db, kind="system", operation="boot.migrate", actor="system",
           actor_kind="system", cost_usd=Decimal("0.01"))
    assert audit.by_actor(fresh_db) == []


def test_the_dearest_person_is_first(fresh_db):
    # The panel is a ranking, so the order is the answer.
    insert(fresh_db, actor="quiet", cost_usd=Decimal("0.001"))
    insert(fresh_db, actor="chatty", cost_usd=Decimal("0.900"))
    assert [r.actor for r in audit.by_actor(fresh_db)] == ["chatty", "quiet"]


# -- picking a conversation back up ----------------------------------------


def test_a_recent_handoff_carries_what_they_were_looking_at(fresh_db):
    # The handoff message says "ask me here and I will pick it up". Without the
    # context stored, the first follow-up has no antecedent and "can you chart
    # it" gets answered with "which ticker?".
    insert(
        fresh_db, operation="steven.handoff", actor="ehewes", actor_kind="github",
        detail={"discord_user_id": "2807", "context": "Overview: NVDA, score 82"},
    )
    assert audit.last_handoff_context(fresh_db, "2807") == "Overview: NVDA, score 82"


def test_someone_elses_handoff_is_not_picked_up(fresh_db):
    insert(
        fresh_db, operation="steven.handoff", actor="ehewes", actor_kind="github",
        detail={"discord_user_id": "2807", "context": "Overview: NVDA"},
    )
    assert audit.last_handoff_context(fresh_db, "4010") == ""


def test_a_stale_handoff_is_not_picked_up(fresh_db):
    # Answering tomorrow's question against yesterday's screen is worse than
    # asking which ticker.
    fresh_db.execute(
        """
        insert into audit.event (kind, operation, actor, actor_kind, detail, occurred_at)
        values ('agent', 'steven.handoff', 'ehewes', 'github',
                '{"discord_user_id": "2807", "context": "Overview: NVDA"}'::jsonb,
                now() - interval '3 hours')
        """
    )
    assert audit.last_handoff_context(fresh_db, "2807") == ""


def test_the_newest_handoff_wins(fresh_db):
    for context in ("Overview: NVDA", "Audit: spend by person"):
        insert(
            fresh_db, operation="steven.handoff", actor="ehewes", actor_kind="github",
            detail={"discord_user_id": "2807", "context": context},
        )
    assert audit.last_handoff_context(fresh_db, "2807") == "Audit: spend by person"


def test_no_handoff_is_an_empty_string_not_an_error(fresh_db):
    # Most messages to the bot continue nothing, so this is the normal path.
    assert audit.last_handoff_context(fresh_db, "2807") == ""
