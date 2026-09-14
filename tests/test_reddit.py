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


# -- narrowing --------------------------------------------------------------


def spanning(handler):
    """A transport that decides per request, given `(after, before)` seconds."""
    seen: list[httpx.Request] = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        params = request.url.params
        after, before = int(params["after"]), int(params["before"])
        return handler(after, before)

    return httpx.MockTransport(dispatch), seen


def test_a_query_timeout_narrows_the_window_rather_than_asking_again():
    # 422 is the mirror's query giving up, not a rate limit: it reproduces from
    # an address with no request history, and `limit=10` fails exactly as
    # `limit=100` does. Waiting therefore does not help and asking for less
    # does -- the window that refused twelve identical retries was answered on
    # eleven of sixteen first tries once cut into 675-second slices.
    wide = timedelta(hours=6)

    def handler(after, before):
        if before - after > wide.total_seconds():
            return httpx.Response(422, json={"data": None, "error": "Timeout."})
        return httpx.Response(200, json={"data": [post(1)]})

    transport, seen = spanning(handler)
    got = list(
        arctic.items(
            "post", "stocks", after=BASE, before=BASE + timedelta(days=1),
            host=HOST, delay=0.0, sleep=lambda _s: None, transport=transport,
        )
    )
    assert [i.external_id for i in got] == ["t3_p1"]
    # It halved from a day until the mirror answered, rather than re-issuing
    # the same request until a fixed attempt budget ran out.
    widths = [
        int(r.url.params["before"]) - int(r.url.params["after"]) for r in seen
    ]
    assert widths[0] == int(timedelta(days=1).total_seconds())
    assert min(widths) <= wide.total_seconds()
    assert len(set(widths)) > 1


def test_narrowing_below_the_floor_gives_up_rather_than_halving_forever():
    # A stretch of their index that is not answerable at any width. Halving
    # towards zero would never terminate, and the caller writing down an
    # un-walked span is the better answer.
    transport = httpx.MockTransport(
        lambda r: httpx.Response(422, json={"data": None, "error": "Timeout."})
    )
    with pytest.raises(SourceError):
        list(
            arctic.items(
                "post", "stocks", after=BASE, before=BASE + timedelta(days=1),
                host=HOST, delay=0.0, sleep=lambda _s: None, transport=transport,
            )
        )


def test_a_short_page_in_a_narrowed_slice_does_not_end_the_window():
    # The trap a fixed-width walk falls into the moment it narrows: a page
    # shorter than `PAGE` means *this slice* is exhausted, not the window. Read
    # the old way, the first thin slice ends the walk with the rest unread and
    # every count still looking plausible.
    hour = 3600

    def handler(after, before):
        if before - after > 6 * hour:
            return httpx.Response(422, json={"data": None, "error": "Timeout."})
        # One item per slice, always a short page.
        return httpx.Response(200, json={"data": [post(before // 60 % 10000)]})

    transport, seen = spanning(handler)
    got = list(
        arctic.items(
            "post", "stocks", after=BASE, before=BASE + timedelta(days=1),
            host=HOST, delay=0.0, sleep=lambda _s: None, transport=transport,
        )
    )
    # A day read six hours at a time is four slices, not one.
    assert len(got) >= 4
    floors = [int(r.url.params["after"]) for r in seen]
    assert min(floors) == int(BASE.timestamp())


def test_the_width_that_works_is_carried_across_windows():
    # Narrowing costs a run of refusals to discover, and how thin a subreddit is
    # does not change between one day of it and the next. Relearning it per
    # window would pay that cost every day of a backfill.
    hour = 3600

    def handler(after, before):
        if before - after > 6 * hour:
            return httpx.Response(422, json={"data": None, "error": "Timeout."})
        return httpx.Response(200, json={"data": [post(before // 60 % 10000)]})

    transport, seen = spanning(handler)
    list(
        arctic.items(
            "post", "stocks", after=BASE, before=BASE + timedelta(days=3),
            host=HOST, delay=0.0, sleep=lambda _s: None, transport=transport,
        )
    )
    refused = [
        r for r in seen
        if int(r.url.params["before"]) - int(r.url.params["after"]) > 6 * hour
    ]
    # Three days, but only the first one pays to find the width.
    assert len(refused) <= arctic.ATTEMPTS * 8
    first_day_end = int((BASE + timedelta(days=2)).timestamp())
    assert all(int(r.url.params["before"]) > first_day_end for r in refused)


# -- the hole a failed span used to leave ------------------------------------


def _stream(fresh_db, monkeypatch, *, stored, yields, fails, moment):
    """Run one `_walk` over a stream, with `arctic.items` faked.

    Returns the spans it asked for. `yields` are the items the walk produces
    before `fails` (if set) is raised from inside the generator, which is where
    a real interruption happens -- part way through, with the newest slice
    already banked.
    """
    from screener.reddit import ingest as ing
    from screener.reddit.store import source_id as sid

    src = sid(fresh_db)
    if stored:
        save(fresh_db, src, stored)
    asked: list[tuple] = []

    def fake_items(kind, subreddit, *, after, before, **kw):
        asked.append((after, before))
        def gen():
            for it in yields.get((after, before), []):
                yield it
            if fails and (after, before) in yields:
                raise fails
            if fails and not yields:
                raise fails
        return gen()

    monkeypatch.setattr(ing.arctic, "items", fake_items)
    config = RedditConfig(subreddits=("stocks",), backfill_days=7)
    report = ing._walk(
        fresh_db, src, "stocks", "post", config,
        moment=moment, transport=None, sleep=lambda _s: None,
    )
    return src, asked, report


def gaps_in(conn, subreddit="stocks", kind="post"):
    with conn.cursor() as cur:
        cur.execute(
            "select span_after, span_before, reason, attempts from social_gap "
            "where subreddit = %s and kind = %s order by span_before desc",
            [subreddit, kind],
        )
        return cur.fetchall()


def test_a_span_that_dies_halfway_writes_down_what_it_did_not_reach(
    fresh_db, monkeypatch
):
    # The bug this table exists for. The walk runs backwards, so an interrupted
    # span banks everything newer than the point it died at; `max(created_utc)`
    # then jumps to the present and the stretch below is never looked at again.
    # Measured on the box before this existed: `stocks/comment` finished
    # 'partial' on 31 runs of 50, and 36 of the previous 168 hours held no
    # comments while the mirror still held real ones for every hour checked.
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    # A completed backfill, so the old-end span is not what repairs this.
    old = arctic.Item("post", "t3_old", "stocks", "ape",
                      now - timedelta(days=7), 1, "t", "b", None, None)
    last = arctic.Item("post", "t3_last", "stocks", "ape",
                       now - timedelta(hours=6), 1, "t", "b", None, None)
    span = (now - timedelta(hours=6) - ing_overlap(), now)
    # It got two hours back and then the mirror refused.
    partway = [
        arctic.Item("post", f"t3_n{n}", "stocks", "ape",
                    now - timedelta(hours=n), 1, "t", "b", None, None)
        for n in (1, 2)
    ]
    src, asked, report = _stream(
        fresh_db, monkeypatch,
        stored=[old, last], yields={span: partway},
        fails=SourceError("arctic shift timed out answering"), moment=now,
    )
    assert report.failure is not None

    # The un-walked remainder, written down rather than lost: from where the
    # span started up to the oldest item it managed to bank.
    rows = gaps_in(fresh_db)
    assert len(rows) == 1
    assert rows[0][0] == span[0]
    assert rows[0][1] == now - timedelta(hours=2)
    assert rows[0][2] == "interrupted"


def ing_overlap():
    from screener.reddit.ingest import OVERLAP
    return OVERLAP


def test_the_next_pass_walks_the_gap_the_last_one_left(fresh_db, monkeypatch):
    # The half that makes the row worth writing. `latest_seen` and
    # `earliest_seen` are aggregates, so together they describe an interval and
    # neither can say there is a hole inside it.
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    from screener.reddit.store import record_gap, source_id as sid

    src = sid(fresh_db)
    save(fresh_db, src, [
        arctic.Item("post", "t3_old", "stocks", "ape",
                    now - timedelta(days=7), 1, "t", "b", None, None),
        arctic.Item("post", "t3_new", "stocks", "ape",
                    now - timedelta(minutes=10), 1, "t", "b", None, None),
    ])
    hole = (now - timedelta(hours=6), now - timedelta(hours=2))
    record_gap(fresh_db, src, "stocks", "post", after=hole[0], before=hole[1])

    _, asked, report = _stream(
        fresh_db, monkeypatch, stored=[], yields={}, fails=None, moment=now,
    )
    assert hole in asked
    # Fresh comments first: a repair has no upper bound on how long it takes,
    # and nothing should wait behind it.
    assert asked[0][1] == now
    assert asked.index(hole) > 0
    # Walked end to end, so the debt is cleared.
    assert gaps_in(fresh_db) == []


def test_a_gap_that_yields_nothing_is_counted_rather_than_dropped_or_retried_forever(
    fresh_db, monkeypatch
):
    # A span the mirror will not answer at any width. Dropping it forgets a real
    # hole; keeping it untouched puts it at the front of every pass forever.
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    from screener.reddit.store import record_gap, source_id as sid

    src = sid(fresh_db)
    save(fresh_db, src, [
        arctic.Item("post", "t3_old", "stocks", "ape",
                    now - timedelta(days=7), 1, "t", "b", None, None),
        arctic.Item("post", "t3_new", "stocks", "ape",
                    now - timedelta(minutes=10), 1, "t", "b", None, None),
    ])
    hole = (now - timedelta(hours=6), now - timedelta(hours=2))
    record_gap(fresh_db, src, "stocks", "post", after=hole[0], before=hole[1])

    _stream(
        fresh_db, monkeypatch, stored=[], yields={},
        fails=SourceError("arctic shift timed out answering"), moment=now,
    )
    rows = gaps_in(fresh_db)
    assert len(rows) == 1
    assert (rows[0][0], rows[0][1]) == hole
    assert rows[0][3] == 1


def test_a_failing_catch_up_does_not_block_the_repair_behind_it(
    fresh_db, monkeypatch
):
    # `stocks/comment` fails its catch-up more often than not. Stopping the pass
    # at the first failure means the gaps queued behind it are never drained on
    # any pass that would need them -- the spans cover different stretches of
    # the timeline and the mirror refuses them independently.
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    from screener.reddit.store import record_gap, source_id as sid

    src = sid(fresh_db)
    save(fresh_db, src, [
        arctic.Item("post", "t3_old", "stocks", "ape",
                    now - timedelta(days=7), 1, "t", "b", None, None),
        arctic.Item("post", "t3_new", "stocks", "ape",
                    now - timedelta(minutes=10), 1, "t", "b", None, None),
    ])
    hole = (now - timedelta(hours=6), now - timedelta(hours=2))
    record_gap(fresh_db, src, "stocks", "post", after=hole[0], before=hole[1])

    from screener.reddit import ingest as ing

    asked: list[tuple] = []

    def fake_items(kind, subreddit, *, after, before, **kw):
        asked.append((after, before))
        def gen():
            if before == now:
                raise SourceError("arctic shift timed out answering")
            yield arctic.Item("post", "t3_fill", "stocks", "ape",
                              before - timedelta(minutes=1), 1, "t", "b", None, None)
        return gen()

    monkeypatch.setattr(ing.arctic, "items", fake_items)
    ing._walk(
        fresh_db, src, "stocks", "post",
        RedditConfig(subreddits=("stocks",), backfill_days=7),
        moment=now, transport=None, sleep=lambda _s: None,
    )
    assert hole in asked
    assert gaps_in(fresh_db) == []


# -- the repair ---------------------------------------------------------------


def test_queueing_a_backfill_writes_spans_and_opens_no_socket(
    fresh_db, db_url, monkeypatch
):
    # The repair for holes that predate `social_gap`, and deliberately not a
    # hole detector: a quiet hour and a lost hour are identical from here, so
    # this queues the stretch and lets the ordinary drain sort it out.
    from screener.reddit import ingest as ing

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    monkeypatch.setattr(ing, "settings", lambda: _settings_for(db_url))
    monkeypatch.setattr(ing.arctic, "items", _never_called)

    queued = ing.queue(7, RedditConfig(subreddits=("stocks",)), now=now)
    assert sorted(queued) == [("stocks", "comment"), ("stocks", "post")]
    rows = gaps_in(fresh_db, "stocks", "comment")
    assert len(rows) == 1
    assert rows[0][0] == now - timedelta(days=7)
    assert rows[0][2] == "requested"


def test_a_backfill_can_name_one_stream_rather_than_all_four(
    fresh_db, db_url, monkeypatch
):
    # The refusal tracks how thin a subreddit is, so the damage concentrates in
    # one stream while a week of r/wallstreetbets comments is 130,000 items and
    # over an hour of walking that nothing needed.
    from screener.reddit import ingest as ing

    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    monkeypatch.setattr(ing, "settings", lambda: _settings_for(db_url))
    config = RedditConfig(subreddits=("wallstreetbets", "stocks"))

    assert ing.queue(7, config, streams=["stocks/comment"], now=now) == [
        ("stocks", "comment")
    ]
    assert gaps_in(fresh_db, "wallstreetbets", "comment") == []
    assert len(gaps_in(fresh_db, "stocks", "comment")) == 1


def test_a_backfill_for_a_stream_that_is_not_configured_is_refused(
    fresh_db, db_url, monkeypatch
):
    # A typo would otherwise queue a gap nothing ever drains: a row saying data
    # is missing that quietly means nothing is looking for it.
    from screener.reddit import ingest as ing

    monkeypatch.setattr(ing, "settings", lambda: _settings_for(db_url))
    with pytest.raises(ValueError):
        ing.queue(7, RedditConfig(subreddits=("stocks",)), streams=["stonks/comment"])
    with pytest.raises(ValueError):
        ing.queue(7, RedditConfig(subreddits=("stocks",)), streams=["stocks/video"])


def _never_called(*a, **k):
    raise AssertionError("queueing a backfill must not open a socket")


def _settings_for(url):
    from types import SimpleNamespace
    return SimpleNamespace(database_url=url)
