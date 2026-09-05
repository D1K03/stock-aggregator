import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from screener.reddit import RedditConfig, SourceError
from screener.reddit import source as arctic
from screener.reddit.store import content_hash, latest_seen, save, source_id

HOST = "https://arctic.test/api"
BASE = datetime(2026, 9, 1, tzinfo=UTC)


def post(n, *, body="to the moon", title="DD", author="ape", score=10):
    return {
        "id": f"p{n}",
        "subreddit": "wallstreetbets",
        "author": author,
        "created_utc": (BASE + timedelta(minutes=n)).timestamp(),
        "score": score,
        "title": title,
        "selftext": body,
        "permalink": f"/r/wallstreetbets/comments/p{n}/",
    }


def pages(*responses):
    """A transport replaying one JSON body per request, recording each URL."""
    seen: list[httpx.Request] = []
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if not remaining:
            return httpx.Response(200, json={"data": []})
        nxt = remaining.pop(0)
        if isinstance(nxt, int):
            return httpx.Response(nxt, json={"error": "no"})
        return httpx.Response(200, json={"data": nxt})

    return httpx.MockTransport(handler), seen


def walk(*responses, kind="post", delay=0.0, slept=None):
    transport, seen = pages(*responses)
    got = list(
        arctic.items(
            kind, "wallstreetbets",
            after=BASE, before=BASE + timedelta(days=1),
            host=HOST, delay=delay,
            sleep=(slept.append if slept is not None else lambda _s: None),
            transport=transport,
        )
    )
    return got, seen


# -- config -----------------------------------------------------------------


def test_the_subreddit_list_survives_the_spacing_a_person_types(monkeypatch):
    monkeypatch.setenv("REDDIT_SUBREDDITS", " wallstreetbets , r/stocks,investing ")
    assert RedditConfig.from_env().subreddits == ("wallstreetbets", "stocks", "investing")


def test_an_empty_subreddit_list_is_how_ingest_is_switched_off(monkeypatch):
    monkeypatch.setenv("REDDIT_SUBREDDITS", "")
    # Empty falls back to the default rather than meaning "none", so switching
    # off is an explicit comma-free blank the reader can see.
    assert RedditConfig.from_env().enabled is True
    assert RedditConfig(subreddits=()).enabled is False


# -- the walk ---------------------------------------------------------------


def test_a_short_page_ends_the_walk():
    got, seen = walk([post(1), post(2)])
    assert [i.external_id for i in got] == ["t3_p1", "t3_p2"]
    assert len(seen) == 1


def newest_first(*ns):
    """A page as Arctic Shift actually returns one: newest item first."""
    return [post(n) for n in sorted(ns, reverse=True)]


def test_the_walk_moves_backwards_because_the_mirror_answers_newest_first():
    # Measured, not assumed. A forward walk takes the newest hundred, jumps its
    # cursor to the end of the window and stops, so a week's backfill silently
    # returns one page and every count still looks plausible.
    full = newest_first(*range(10, 10 + arctic.PAGE))
    got, seen = walk(full, newest_first(1))
    assert len(got) == arctic.PAGE + 1
    assert len(seen) == 2
    # The second request ends where the first one's oldest item was.
    oldest = min(i.created_utc for i in got[: arctic.PAGE])
    assert f"before={int(oldest.timestamp())}" in str(seen[1].url)
    # And its lower edge never moves: that is the window the caller asked for.
    assert f"after={int(BASE.timestamp())}" in str(seen[1].url)


def test_an_item_repeated_across_a_page_boundary_is_only_yielded_once():
    # The edge is inclusive, so the item it lands on comes back on both pages.
    # Yielding it twice would double a count at every page boundary.
    full = newest_first(*range(10, 10 + arctic.PAGE))
    got, _ = walk(full, newest_first(10, 1))
    assert len(got) == arctic.PAGE + 1
    assert len({i.external_id for i in got}) == len(got)


def test_a_page_that_cannot_move_the_edge_does_not_loop_forever():
    # More than a page of items sharing one second. Stepping past loses some of
    # that second, but not stepping never terminates, and a run that hangs is
    # the worse failure.
    same = [dict(post(n), created_utc=(BASE + timedelta(hours=5)).timestamp())
            for n in range(arctic.PAGE)]
    # A different id, or it is deduped as a repeat rather than counted.
    got, seen = walk(same, newest_first(500))
    assert len(seen) == 2
    assert len(got) == arctic.PAGE + 1


def test_a_rate_limit_backs_off_and_retries():
    slept: list[float] = []
    got, seen = walk(429, [post(1)], delay=0.5, slept=slept)
    assert [i.external_id for i in got] == ["t3_p1"]
    assert slept and slept[0] == 1.0


def test_a_body_that_is_not_a_listing_is_refused_rather_than_half_walked():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>hi</html>"))
    with pytest.raises(SourceError):
        list(
            arctic.items(
                "post", "stocks", after=BASE, before=BASE + timedelta(days=1),
                host=HOST, sleep=lambda _s: None, transport=transport,
            )
        )


def test_a_deleted_author_is_stored_as_nothing_rather_than_as_a_username():
    got, _ = walk([post(1, author="[deleted]")])
    assert got[0].author is None


def test_a_comment_with_no_body_is_dropped_but_a_titled_post_is_kept():
    got, _ = walk([post(1, body=""), post(2, body="", title="")])
    assert [i.external_id for i in got] == ["t3_p1"]


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        list(arctic.items("video", "stocks", after=BASE, before=BASE, host=HOST))


# -- storage ----------------------------------------------------------------


def test_the_hash_ignores_the_vote_count_so_a_re_fetch_is_not_an_edit():
    # The Yahoo lesson applied: hashing a field that moves on every fetch means
    # the dedup never fires. Score moves every time anyone votes.
    a = arctic.Item("post", "t3_a", "stocks", "ape", BASE, 1, "T", "body", None, None)
    b = arctic.Item("post", "t3_a", "stocks", "ape", BASE, 9999, "T", "body", None, None)
    assert content_hash(a) == content_hash(b)


def test_the_hash_changes_when_the_words_do():
    a = arctic.Item("post", "t3_a", "stocks", "ape", BASE, 1, "T", "body", None, None)
    b = arctic.Item("post", "t3_a", "stocks", "ape", BASE, 1, "T", "edited", None, None)
    assert content_hash(a) != content_hash(b)


def item(n, body="hello"):
    return arctic.Item(
        "post", f"t3_x{n}", "stocks", "ape",
        BASE + timedelta(minutes=n), 5, "title", body, "/r/stocks/x", None,
    )


def test_an_item_round_trips_and_re_ingesting_it_writes_nothing(fresh_db):
    src = source_id(fresh_db)
    assert save(fresh_db, src, [item(1), item(2)]) == (2, 0)

    with fresh_db.cursor() as cur:
        cur.execute("select fetched_at from social_item where external_id = 't3_x1'")
        first_seen = cur.fetchone()[0]

    # The claim that distinguishes this from Yahoo: the same text again is not
    # an edit, so nothing is written and the counts say so. An earlier version
    # counted the rows that existed afterwards, which is the whole batch every
    # time, and could not tell a working dedup from a broken one.
    assert save(fresh_db, src, [item(1), item(2)]) == (0, 0)
    with fresh_db.cursor() as cur:
        cur.execute("select count(*), max(fetched_at) from social_item")
        count, latest = cur.fetchone()
    assert count == 2
    assert latest == first_seen


def test_an_edited_body_updates_the_row_rather_than_adding_one(fresh_db):
    src = source_id(fresh_db)
    save(fresh_db, src, [item(1)])
    assert save(fresh_db, src, [item(1, body="actually the opposite")]) == (0, 1)
    with fresh_db.cursor() as cur:
        cur.execute("select count(*), body from social_item group by body")
        rows = cur.fetchall()
    assert rows == [(1, "actually the opposite")]


def test_latest_seen_is_asked_per_subreddit_so_a_new_one_still_backfills(fresh_db):
    src = source_id(fresh_db)
    save(fresh_db, src, [item(1)])
    assert latest_seen(fresh_db, src, "stocks", "post") is not None
    assert latest_seen(fresh_db, src, "wallstreetbets", "post") is None


def test_the_migration_seeded_the_source(fresh_db):
    assert source_id(fresh_db) > 0


def test_the_mirrors_own_slow_down_signal_is_a_throttle_not_a_failure():
    # Arctic Shift answers 422 with {"error": "Timeout. Maybe slow down a bit"}.
    # Treating that as fatal ends a backfill about twelve pages in, with most of
    # the window unread and a count that still looks plausible.
    slept: list[float] = []
    got, _ = walk(422, newest_first(1), delay=0.5, slept=slept)
    assert [i.external_id for i in got] == ["t3_p1"]
    assert slept and slept[0] == 1.0


def test_a_batch_of_new_and_unchanged_is_counted_apart(fresh_db):
    src = source_id(fresh_db)
    save(fresh_db, src, [item(1), item(2)])
    # One unchanged, one edited, one new.
    inserted, edited = save(
        fresh_db, src, [item(1), item(2, body="changed my mind"), item(3)]
    )
    assert (inserted, edited) == (1, 1)


def test_an_interrupted_backfill_is_finished_by_the_next_pass(fresh_db, monkeypatch):
    # The walk runs backwards from now, so an interruption leaves the newest
    # slice stored and the older end missing. Resuming from the newest item
    # alone would never come back for it, and on a busy subreddit the mirror
    # refuses about one page in six, so this is the ordinary case rather than
    # the unlucky one.
    from datetime import timedelta

    from screener.reddit import ingest as ing
    from screener.reddit.store import earliest_seen, source_id

    src = source_id(fresh_db)
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)

    # A first pass that only reached two days into a seven day window.
    partial = [
        arctic.Item("post", f"t3_a{n}", "stocks", "ape",
                    now - timedelta(days=1, minutes=n), 1, "t", "b", None, None)
        for n in range(3)
    ]
    save(fresh_db, src, partial)
    stored_oldest = earliest_seen(fresh_db, src, "stocks", "post")
    assert stored_oldest is not None and stored_oldest > now - timedelta(days=7)

    asked: list[tuple] = []

    def fake_items(kind, subreddit, *, after, before, **kw):
        asked.append((after, before))
        return iter(())

    monkeypatch.setattr(ing.arctic, "items", fake_items)
    config = RedditConfig(subreddits=("stocks",), backfill_days=7)
    ing._walk(
        fresh_db, src, "stocks", "post", config,
        moment=now, transport=None, sleep=lambda _s: None,
    )

    # Two spans: catch up to now, and go back for what was missed.
    assert len(asked) == 2
    catch_up, gap = asked
    assert catch_up[1] == now
    assert gap[0] == now - timedelta(days=7)
    assert gap[1] == stored_oldest


def test_a_complete_backfill_does_not_keep_re_walking_the_old_end(fresh_db, monkeypatch):
    from datetime import timedelta

    from screener.reddit import ingest as ing
    from screener.reddit.store import source_id

    src = source_id(fresh_db)
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    # Something at the far edge of the window, so the target is reached.
    save(fresh_db, src, [
        arctic.Item("post", "t3_old", "stocks", "ape",
                    now - timedelta(days=7), 1, "t", "b", None, None)
    ])

    asked: list[tuple] = []
    monkeypatch.setattr(
        ing.arctic, "items",
        lambda kind, sub, *, after, before, **kw: (asked.append((after, before)), iter(()))[1],
    )
    ing._walk(
        fresh_db, src, "stocks", "post", RedditConfig(subreddits=("stocks",), backfill_days=7),
        moment=now, transport=None, sleep=lambda _s: None,
    )
    assert len(asked) == 1
