"""The daily spend cap, and folding two identities into one person.

A reply costs a few hundredths of a penny, so none of this is about saving
money in normal use. It is about a loop, a script, or someone holding down
enter — where without a ceiling the first anyone knows is the invoice.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from screener.audit import fold
from screener.audit.models import ActorSpend
from screener.audit.people import avatar
from screener.bot import budget

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def spend(actor: str, kind: str, cost: str, *, tokens: int = 100, when=NOW) -> ActorSpend:
    return ActorSpend(
        actor=actor, actor_kind=kind, events=1, cost=Decimal(cost),
        tokens=tokens, cost_24h=Decimal(cost), last_seen=when,
    )


# -- the cap ---------------------------------------------------------------


def test_the_cap_defaults_to_ten_cents_a_day(monkeypatch):
    # Roughly two thousand replies at current prices: far more than two people
    # can read, far less than a runaway loop can spend.
    monkeypatch.delenv("DAILY_SPEND_CAP_USD", raising=False)
    assert budget.daily_cap() == Decimal("0.10")


def test_the_cap_can_be_set(monkeypatch):
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "2.50")
    assert budget.daily_cap() == Decimal("2.50")


def test_a_cap_that_is_not_a_number_falls_back_rather_than_crashing(monkeypatch):
    # A typo in a deployment variable should not stop the bot answering, and it
    # must not silently become no limit either.
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "ten pence")
    assert budget.daily_cap() == Decimal("0.10")


def test_a_zero_cap_stops_everyone(monkeypatch):
    # The same reading an empty ALLOWED_DISCORD_USER_IDS gets: the permissive
    # interpretation turns a mistyped variable into no limit at all, which is
    # the one outcome a cap exists to prevent.
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "0")
    monkeypatch.setattr(budget, "spent_24h", lambda a, k: Decimal("0"))
    assert budget.check("ehewes", "github").allowed is False


def test_spend_under_the_cap_is_allowed(monkeypatch):
    monkeypatch.delenv("DAILY_SPEND_CAP_USD", raising=False)
    monkeypatch.setattr(budget, "spent_24h", lambda a, k: Decimal("0.09"))
    allowance = budget.check("ehewes", "github")
    assert allowance.allowed is True
    assert allowance.remaining == Decimal("0.01")


def test_spend_at_the_cap_is_refused(monkeypatch):
    # At, not over: the request being checked has not been paid for yet, and
    # letting it through is how a cap becomes a suggestion.
    monkeypatch.delenv("DAILY_SPEND_CAP_USD", raising=False)
    monkeypatch.setattr(budget, "spent_24h", lambda a, k: Decimal("0.10"))
    assert budget.check("ehewes", "github").allowed is False


def test_an_unreadable_trail_answers_anyway(monkeypatch):
    # Fails open, like everything else in the audit layer. Refusing everyone
    # because Postgres blinked is worse than a few unmetered replies.
    monkeypatch.setattr(budget, "spent_24h", lambda a, k: None)
    assert budget.check("ehewes", "github").allowed is True


def test_system_work_is_never_capped(monkeypatch):
    # Nobody is holding down enter on a scheduled job, and refusing one on a
    # budget meant for chat is a surprise at the worst possible time.
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "0")
    assert budget.check("system", "system").allowed is True


def test_the_cap_cannot_be_doubled_by_switching_surface(monkeypatch):
    # The same person asking in Discord instead of the dashboard is the same
    # person, and the mapping to prove it already exists for the handoff.
    monkeypatch.setenv("DISCORD_USER_MAP", "ehewes:2807,D1K03:4010")
    assert set(budget.identities("ehewes", "github")) == {
        ("ehewes", "github"), ("2807", "discord"),
    }
    assert set(budget.identities("4010", "discord")) == {
        ("4010", "discord"), ("d1k03", "github"),
    }


def test_an_unmapped_account_is_still_metered(monkeypatch):
    monkeypatch.setenv("DISCORD_USER_MAP", "ehewes:2807")
    assert budget.identities("9999", "discord") == [("9999", "discord")]


# -- folding identities into people ----------------------------------------


def test_two_identities_for_one_person_become_one_row():
    rows = [spend("ehewes", "github", "0.04"), spend("2807", "discord", "0.02")]
    people = fold(rows, {"2807": "ehewes"})
    assert len(people) == 1
    assert people[0].login == "ehewes"
    assert people[0].cost == Decimal("0.06")
    assert people[0].events == 2
    # Where the money went is kept, dearest surface first.
    assert people[0].surfaces == (("github", Decimal("0.04")), ("discord", Decimal("0.02")))


def test_a_login_is_matched_whatever_its_casing():
    # GitHub logins are case-insensitive and a session carries whatever casing
    # its owner chose, so folding on the raw string would split one person.
    rows = [spend("D1K03", "github", "0.03"), spend("4010", "discord", "0.01")]
    people = fold(rows, {"4010": "d1k03"})
    assert len(people) == 1
    # Displayed as they spell it, not as the mapping happens to.
    assert people[0].login == "D1K03"


def test_someone_with_no_mapping_still_appears():
    # A bare Discord id is a worse answer than dropping them, and a much better
    # one than silently attributing their spend to nobody.
    people = fold([spend("55501", "discord", "0.01")], {})
    assert [p.login for p in people] == ["55501"]
    assert people[0].known is False


def test_an_unmapped_id_cannot_collide_with_a_login():
    # Keyed by kind as well as name, so a Discord id that happens to read like
    # a username does not merge two strangers into one bill.
    rows = [spend("9000", "discord", "0.01"), spend("9000", "github", "0.02")]
    people = fold(rows, {})
    assert len(people) == 2


def test_people_are_ranked_by_what_they_cost():
    rows = [spend("quiet", "github", "0.001"), spend("chatty", "github", "0.900")]
    assert [p.login for p in fold(rows, {})] == ["chatty", "quiet"]


def test_the_most_recent_use_across_surfaces_wins():
    rows = [
        spend("ehewes", "github", "0.01", when=NOW - timedelta(hours=5)),
        spend("2807", "discord", "0.01", when=NOW),
    ]
    assert fold(rows, {"2807": "ehewes"})[0].last_seen == NOW


def test_only_a_github_identity_gets_a_picture():
    known, unknown = fold(
        [spend("ehewes", "github", "0.02"), spend("55501", "discord", "0.01")], {}
    )
    assert avatar(known) == "https://github.com/ehewes.png?size=80"
    assert avatar(unknown) is None


def test_the_local_development_login_does_not_borrow_a_strangers_face():
    # /auth/local issues "local-dev", which is not a GitHub account — except
    # that somebody holds github.com/local-dev, so the picture loaded happily
    # and belonged to a person with no connection to this at all.
    (local,) = fold([spend("local-dev", "github", "0.01")], {})
    assert avatar(local) is None


@pytest.mark.parametrize("kind", ["github", "discord"])
def test_folding_an_empty_trail_is_an_empty_list(kind):
    assert fold([], {"2807": "ehewes"}) == []


# -- the query itself ------------------------------------------------------


def test_the_spend_query_actually_runs(fresh_db, monkeypatch):
    # Every test above stubs `spent_24h`, which is how the first version of it
    # shipped with a row comparison Postgres rejects: the cap failed open in
    # silence because nothing ever executed the SQL. This runs it.
    monkeypatch.setenv("DISCORD_USER_MAP", "ehewes:2807")
    fresh_db.execute(
        """
        insert into audit.event (kind, operation, actor, actor_kind, cost_usd)
        values ('agent', 'steven.reply', 'ehewes', 'github', 0.03),
               ('agent', 'steven.reply', '2807', 'discord', 0.02),
               ('agent', 'steven.reply', 'someone-else', 'github', 0.99)
        """
    )
    # Both surfaces of one person, and nobody else's.
    assert budget.spent_24h("ehewes", "github", conn=fresh_db) == Decimal("0.05")
    assert budget.spent_24h("2807", "discord", conn=fresh_db) == Decimal("0.05")


def test_charges_older_than_a_day_have_aged_out(fresh_db, monkeypatch):
    # A rolling window, so yesterday's questions do not hold today's budget.
    monkeypatch.setenv("DISCORD_USER_MAP", "")
    fresh_db.execute(
        """
        insert into audit.event (kind, operation, actor, actor_kind, cost_usd, occurred_at)
        values ('agent', 'steven.reply', 'ehewes', 'github', 0.40, now() - interval '30 hours'),
               ('agent', 'steven.reply', 'ehewes', 'github', 0.01, now())
        """
    )
    assert budget.spent_24h("ehewes", "github", conn=fresh_db) == Decimal("0.01")


def test_an_id_that_looks_like_a_login_is_not_the_same_person(fresh_db, monkeypatch):
    # Matched as pairs rather than two `in` lists, or a Discord id that happened
    # to equal somebody's username would spend their budget.
    monkeypatch.setenv("DISCORD_USER_MAP", "")
    fresh_db.execute(
        """
        insert into audit.event (kind, operation, actor, actor_kind, cost_usd)
        values ('agent', 'steven.reply', '9000', 'discord', 0.05),
               ('agent', 'steven.reply', '9000', 'github', 0.07)
        """
    )
    assert budget.spent_24h("9000", "discord", conn=fresh_db) == Decimal("0.05")


def test_a_tiny_cap_is_not_reported_as_zero():
    # Two decimal places would render a hundredth of a cent as "$0.00", which
    # reads as a bug in the very message meant to explain the refusal.
    assert budget.usd(Decimal("0.0001")) == "$0.0001"
    assert budget.usd(Decimal("0.10")) == "$0.10"
    assert budget.usd(Decimal("2.5")) == "$2.50"
