"""Documents and attempts, against a real schema.

The property worth most here is the one `screener.reddit` paid for: a re-fetch
that found the same article must write nothing at all. Yahoo's whole-response
hash changed every night and the dedup it existed for never fired once, so this
asserts the fix rather than assuming it.
"""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from screener.blobs import LocalStore
from screener.magpie import store
from screener.magpie.models import Extracted

BODY = " ".join(f"word{i}" for i in range(120))


def article(title: str = "A headline", text: str = BODY) -> Extracted:
    return Extracted(title=title, text=text, author="A Reporter",
                     published=date(2026, 9, 1), canonical_url="https://e.com/real")


@pytest.fixture
def blobs(tmp_path):
    return LocalStore(tmp_path)


def put(conn, blobs, url: str, piece: Extracted):
    digest = store.content_hash(piece)
    path = store.put_payload(blobs, store.host_of(url), date.today(), digest, "<html></html>")
    return store.save(conn, url=url, article=piece, strategy="direct", status_code=200,
                      blob_path=path, digest=digest, requested_by="ehewes")


def test_a_document_round_trips(fresh_db, blobs):
    saved = put(fresh_db, blobs, "https://e.com/a", article())
    assert saved.stored is True
    assert saved.title == "A headline"
    assert saved.word_count == 120
    assert store.recent(fresh_db)[0].id == saved.id


def test_re_scraping_an_unchanged_article_writes_nothing(fresh_db, blobs):
    # The reddit lesson, as a property. An unchanged re-fetch that rewrote the
    # row would make every pass look like an edit, and the dedup would be
    # invisible in the logs — which is exactly how Yahoo's went unnoticed.
    first = put(fresh_db, blobs, "https://e.com/a", article())
    again = put(fresh_db, blobs, "https://e.com/a", article())
    assert first.id == again.id
    assert again.stored is False
    assert first.fetched_at == again.fetched_at


def test_a_rewritten_headline_updates_the_row_rather_than_adding_one(fresh_db, blobs):
    first = put(fresh_db, blobs, "https://e.com/a", article())
    edited = put(fresh_db, blobs, "https://e.com/a", article(title="A better headline"))
    assert edited.id == first.id
    assert edited.stored is True
    assert store.recent(fresh_db)[0].title == "A better headline"


def test_the_hash_ignores_what_is_not_the_article(fresh_db, blobs):
    # Two fetches of one page differ by their advertising, their nonces and
    # their render timestamp. The headline and the body do not, and those are
    # what is hashed — so the same article in different furniture is one row.
    piece = article()
    assert store.content_hash(piece) == store.content_hash(article())
    assert store.content_hash(piece) != store.content_hash(article(title="Other"))


def test_two_links_differing_only_in_tracking_are_one_document(fresh_db, blobs):
    put(fresh_db, blobs, "https://e.com/a?utm_source=newsletter", article())
    put(fresh_db, blobs, "https://e.com/a?fbclid=xyz", article())
    assert len(store.recent(fresh_db)) == 1


def test_a_syndicated_copy_at_another_address_is_its_own_document(fresh_db, blobs):
    # Keyed on the URL rather than the hash, on purpose: a unique constraint on
    # the text would reject a republication that is legitimately a second page.
    put(fresh_db, blobs, "https://one.com/a", article())
    put(fresh_db, blobs, "https://two.com/a", article())
    assert len(store.recent(fresh_db)) == 2


def test_the_page_as_fetched_is_kept_beside_the_text(fresh_db, blobs, tmp_path):
    # Evidence, not cache. When an extraction looks wrong the only way to tell
    # whether the site changed or the parser did is to have the bytes.
    saved = put(fresh_db, blobs, "https://e.com/a", article())
    assert saved.blob_path.startswith("magpie/e.com/")
    assert (tmp_path / saved.blob_path).exists()


def test_a_host_cannot_smuggle_a_path_into_the_blob_store():
    # A host is the only part of a URL that reaches a path, and `blobs.check`
    # refuses anything the SigV4 signer would have to percent-encode. Sanitised
    # rather than trusted, so a strange host cannot make a path that escapes the
    # prefix or one that the signer would silently mangle.
    path = store.blob_path("../../etc", date.today(), b"\x00" * 32)
    assert path.startswith("magpie/")
    assert ".." not in path
    assert path.count("/") == 3


# -- attempts --------------------------------------------------------------


def test_a_refusal_is_recorded_so_a_dead_link_is_not_fetched_twice(fresh_db):
    # Without this, a link robots.txt forbids is re-requested, re-fetched and
    # refused again every single time anybody asks about it.
    attempt = store.begin_attempt(fresh_db, url="https://e.com/private/a", requested_by="ehewes")
    store.finish_attempt(fresh_db, attempt, state="refused", reason="robots",
                         error="robots.txt disallows it")
    assert store.last_refusal(fresh_db, "https://e.com/private/a") == (
        "robots", "robots.txt disallows it",
    )


def test_a_refusal_is_found_through_a_tracking_link_too(fresh_db):
    attempt = store.begin_attempt(fresh_db, url="https://e.com/x")
    store.finish_attempt(fresh_db, attempt, state="refused", reason="paywall", error="not free")
    assert store.last_refusal(fresh_db, "https://e.com/x?utm_source=mail") is not None


def test_a_successful_fetch_is_not_a_refusal(fresh_db):
    attempt = store.begin_attempt(fresh_db, url="https://e.com/a")
    store.finish_attempt(fresh_db, attempt, state="stored", strategy="direct")
    assert store.last_refusal(fresh_db, "https://e.com/a") is None


def test_the_meter_counts_only_billed_fetches_in_the_last_day(fresh_db):
    for strategy in ("direct", "isp_proxy", "unlocker", "unlocker"):
        attempt = store.begin_attempt(fresh_db, url=f"https://e.com/{strategy}")
        store.finish_attempt(fresh_db, attempt, state="stored", strategy=strategy,
                             cost_usd=Decimal("0.002") if strategy == "unlocker" else Decimal(0))
    assert store.unlocker_used_today(fresh_db) == 2


def test_an_unsettled_attempt_is_left_rather_than_failing_the_scrape(fresh_db):
    # `finish_attempt` never raises: the caller is in the middle of answering
    # somebody, and losing a row is smaller than losing the reply.
    store.finish_attempt(fresh_db, 999_999, state="stored")


def test_an_attempt_is_opened_before_anything_is_fetched(fresh_db):
    # So a process that dies mid-fetch leaves a row saying what it was doing,
    # which is how skybird tells a capture that failed from one never asked for.
    attempt = store.begin_attempt(fresh_db, url="https://e.com/a")
    with fresh_db.cursor() as cur:
        cur.execute("select state, host from magpie.attempt where id = %s", (attempt,))
        assert cur.fetchone() == ("running", "e.com")


def test_an_attempt_left_by_a_dead_process_is_settled_on_the_next_start(fresh_db):
    # The first time this was needed, a container that could not write a blob
    # left two attempts reading 'running' for ever, and nothing in the interface
    # could tell them from a fetch still in flight.
    stuck = store.begin_attempt(fresh_db, url="https://e.com/a")
    finished = store.begin_attempt(fresh_db, url="https://e.com/b")
    store.finish_attempt(fresh_db, finished, state="stored", strategy="direct")

    assert store.reconcile(fresh_db) == 1
    with fresh_db.cursor() as cur:
        cur.execute("select state, reason from magpie.attempt where id = %s", (stuck,))
        # `restarted`, not `all_strategies_failed`. No route was tried, and
        # saying every route failed sends the next person looking at the web
        # when the answer is a deploy. That is not hypothetical: it is what a
        # deploy replacing the container mid-fetch reported on the day this
        # shipped.
        assert cur.fetchone() == ("failed", "restarted")
        cur.execute("select state from magpie.attempt where id = %s", (finished,))
        assert cur.fetchone() == ("stored",)


def test_deleting_a_document_keeps_the_attempts_that_produced_it(fresh_db, blobs):
    # The record that a page was fetched, by whom and at what cost outlives the
    # decision to stop keeping its text. Without it a link that had already been
    # paid for would look like one nobody had tried.
    saved = put(fresh_db, blobs, "https://e.com/a", article())
    attempt = store.begin_attempt(fresh_db, url="https://e.com/a", requested_by="ehewes")
    store.finish_attempt(fresh_db, attempt, state="stored", strategy="unlocker",
                         cost_usd=Decimal("0.002"), document_id=saved.id)

    assert store.forget(fresh_db, saved.id) is True
    assert store.recent(fresh_db) == []
    with fresh_db.cursor() as cur:
        cur.execute("select state, cost_usd, document_id from magpie.attempt where id = %s",
                    (attempt,))
        state, cost, document_id = cur.fetchone()
    assert (state, float(cost)) == ("stored", 0.002)
    assert document_id is None  # on delete set null, not cascade


def test_deleting_something_that_is_not_there_says_so(fresh_db):
    assert store.forget(fresh_db, 999_999) is False


def test_a_deleted_document_can_be_scraped_again(fresh_db, blobs):
    first = put(fresh_db, blobs, "https://e.com/a", article())
    store.forget(fresh_db, first.id)
    again = put(fresh_db, blobs, "https://e.com/a", article())
    assert again.stored is True
    assert again.id != first.id


# -- the sites a document points at ----------------------------------------


def refs(*pairs) -> list:
    from screener.magpie.models import Reference
    from screener.magpie.urls import host_of

    return [
        Reference(url=u, host=host_of(u), anchor=a, occurrences=n)
        for u, a, n in pairs
    ]


def test_links_are_stored_and_read_back_grouped_by_site(fresh_db, blobs):
    saved = put(fresh_db, blobs, "https://news.com/a", article())
    store.save_links(fresh_db, saved.id, refs(
        ("https://doi.org/1", "A Study", 2),
        ("https://doi.org/2", "Another Study", 1),
        ("https://sec.gov/x", "The Rule", 1),
    ))
    sites = store.sites_for(fresh_db, saved.id)
    assert [s["host"] for s in sites] == ["doi.org", "sec.gov"]
    assert sites[0]["links"] == 2 and sites[0]["mentions"] == 3
    assert sites[0]["followed"] == 0


def test_a_document_is_expanded_once_and_then_remembered(fresh_db, blobs):
    # The stamp rather than "are there rows": an article that genuinely cites
    # nothing and one nobody has opened are different states, and without the
    # stamp the stored page is parsed again on every page view.
    saved = put(fresh_db, blobs, "https://news.com/a", article())
    assert store.links_read(fresh_db, saved.id) is False
    store.save_links(fresh_db, saved.id, [])
    assert store.links_read(fresh_db, saved.id) is True
    assert store.links_for(fresh_db, saved.id) == []


def test_re_reading_a_page_updates_counts_rather_than_doubling_them(fresh_db, blobs):
    saved = put(fresh_db, blobs, "https://news.com/a", article())
    store.save_links(fresh_db, saved.id, refs(("https://doi.org/1", "A Study", 1)))
    store.save_links(fresh_db, saved.id, refs(("https://doi.org/1", "A Study", 3)))
    links = store.links_for(fresh_db, saved.id)
    assert len(links) == 1 and links[0]["occurrences"] == 3


def test_links_can_be_narrowed_to_one_site(fresh_db, blobs):
    saved = put(fresh_db, blobs, "https://news.com/a", article())
    store.save_links(fresh_db, saved.id, refs(
        ("https://doi.org/1", "A", 1), ("https://sec.gov/x", "B", 1),
    ))
    assert [l["host"] for l in store.links_for(fresh_db, saved.id, "sec.gov")] == ["sec.gov"]


def test_following_a_link_records_which_document_it_produced(fresh_db, blobs):
    source = put(fresh_db, blobs, "https://news.com/a", article())
    store.save_links(fresh_db, source.id, refs(("https://doi.org/1", "A Study", 1)))
    produced = put(fresh_db, blobs, "https://doi.org/1", article(title="A Study"))

    link_id = store.links_for(fresh_db, source.id)[0]["id"]
    store.mark_followed(fresh_db, link_id, produced.id)

    assert store.links_for(fresh_db, source.id)[0]["scraped_id"] == produced.id
    assert store.sites_for(fresh_db, source.id)[0]["followed"] == 1


def test_deleting_a_document_takes_its_links_with_it(fresh_db, blobs):
    # Cascade, unlike attempts. An attempt records that money was spent and a
    # site was asked; a link is a reading of a page, and once the page is gone
    # the reading is not evidence of anything.
    saved = put(fresh_db, blobs, "https://news.com/a", article())
    store.save_links(fresh_db, saved.id, refs(("https://doi.org/1", "A", 1)))
    store.forget(fresh_db, saved.id)
    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from magpie.link")
        assert cur.fetchone() == (0,)


def test_deleting_a_followed_document_leaves_the_link_unfollowed(fresh_db, blobs):
    # Set null rather than cascade: deleting what a link produced should leave
    # the link, ready to be followed again, not delete the reference to it.
    source = put(fresh_db, blobs, "https://news.com/a", article())
    store.save_links(fresh_db, source.id, refs(("https://doi.org/1", "A", 1)))
    produced = put(fresh_db, blobs, "https://doi.org/1", article(title="A"))
    link_id = store.links_for(fresh_db, source.id)[0]["id"]
    store.mark_followed(fresh_db, link_id, produced.id)

    store.forget(fresh_db, produced.id)
    remaining = store.links_for(fresh_db, source.id)
    assert len(remaining) == 1 and remaining[0]["scraped_id"] is None


def test_the_frontier_is_the_links_nobody_has_followed(fresh_db, blobs):
    # What a crawler would drain. It exists before anything drains it so that
    # cycle is a new process rather than a migration over live rows.
    saved = put(fresh_db, blobs, "https://news.com/a", article())
    store.save_links(fresh_db, saved.id, refs(
        ("https://doi.org/1", "A", 1), ("https://sec.gov/x", "B", 1),
    ))
    followed = put(fresh_db, blobs, "https://doi.org/1", article(title="A"))
    store.mark_followed(fresh_db, store.links_for(fresh_db, saved.id)[0]["id"], followed.id)

    with fresh_db.cursor() as cur:
        cur.execute("select count(*) from magpie.link where scraped_id is null")
        assert cur.fetchone() == (1,)


def test_a_document_reads_back_whole_with_its_text(fresh_db, blobs):
    saved = put(fresh_db, blobs, "https://news.com/a", article())
    read = store.document(fresh_db, saved.id)
    assert read is not None and read.text == saved.text and read.blob_path == saved.blob_path
    assert store.document(fresh_db, 999_999) is None


# -- reading the stored page back ------------------------------------------


def test_expanding_reads_the_stored_page_and_not_the_site(fresh_db, blobs, monkeypatch):
    # The payoff for keeping payloads: the sources cost no request. Nothing in
    # this project read a blob back before this, and the guard matters because
    # trafilatura ships a downloader that would work and route around both the
    # ladder and the daily meter.
    import gzip

    from screener.magpie import run
    from screener.magpie.store import blob_path

    page = f"""<html><head><title>T</title></head><body><article>
      <p>{BODY} see <a href="https://doi.org/1">A Study</a>.</p>
      <p>{BODY} and <a href="https://sec.gov/x">The Rule</a>.</p>
    </article></body></html>"""

    piece = article()
    digest = store.content_hash(piece)
    path = blob_path("news.com", date.today(), digest)
    blobs.put(path, gzip.compress(page.encode()))
    saved = store.save(fresh_db, url="https://news.com/a", article=piece,
                       strategy="direct", status_code=200, blob_path=path, digest=digest)

    def explode(*args, **kwargs):
        raise AssertionError("expanding opened a socket")

    monkeypatch.setattr(httpx.Client, "send", explode)

    assert run.expand(saved.id, conn=fresh_db, blobs=blobs) == 2
    assert {s["host"] for s in store.sites_for(fresh_db, saved.id)} == {"doi.org", "sec.gov"}


def test_expanding_twice_reads_the_page_once(fresh_db, blobs):
    import gzip

    from screener.magpie import run
    from screener.magpie.store import blob_path

    page = f"<html><body><article><p>{BODY} <a href='https://doi.org/1'>A</a></p></article></body></html>"
    piece = article()
    digest = store.content_hash(piece)
    path = blob_path("news.com", date.today(), digest)
    blobs.put(path, gzip.compress(page.encode()))
    saved = store.save(fresh_db, url="https://news.com/a", article=piece,
                       strategy="direct", status_code=200, blob_path=path, digest=digest)

    run.expand(saved.id, conn=fresh_db, blobs=blobs)

    reads: list[str] = []
    original = blobs.get
    blobs.get = lambda p: (reads.append(p), original(p))[1]  # type: ignore[method-assign]
    run.expand(saved.id, conn=fresh_db, blobs=blobs)
    assert reads == []


def test_a_document_whose_stored_page_is_gone_says_so(fresh_db, blobs):
    # Locally the blobs live inside the container and a rebuild takes them with
    # it. The document is still readable, so this is not fatal to the page.
    from screener.magpie import run

    saved = store.save(
        fresh_db, url="https://news.com/a", article=article(), strategy="direct",
        status_code=200, blob_path="magpie/news.com/2020-01-01/deadbeef.html.gz",
        digest=store.content_hash(article()),
    )
    with pytest.raises(run.PageGone):
        run.expand(saved.id, conn=fresh_db, blobs=blobs)


def test_expanding_something_that_is_not_there_says_so(fresh_db, blobs):
    from screener.magpie import run

    with pytest.raises(run.PageGone):
        run.expand(999_999, conn=fresh_db, blobs=blobs)


def test_re_reading_a_page_does_not_forget_what_was_already_followed(fresh_db, blobs):
    # The upsert touches the words and the count and nothing else. If it wrote
    # the whole row a document re-read after an edit would silently drop every
    # follow, and the frontier would refill with links already fetched and paid
    # for.
    source = put(fresh_db, blobs, "https://news.com/a", article())
    store.save_links(fresh_db, source.id, refs(("https://doi.org/1", "A Study", 1)))
    produced = put(fresh_db, blobs, "https://doi.org/1", article(title="A Study"))
    link_id = store.links_for(fresh_db, source.id)[0]["id"]
    store.mark_followed(fresh_db, link_id, produced.id)

    store.save_links(fresh_db, source.id, refs(("https://doi.org/1", "A Study, revised", 4)))

    link = store.links_for(fresh_db, source.id)[0]
    assert link["scraped_id"] == produced.id
    assert link["anchor"] == "A Study, revised" and link["occurrences"] == 4


def test_a_page_that_cannot_be_kept_settles_the_attempt_rather_than_stranding_it(fresh_db):
    # The fetch worked and the store did not, which is the case none of the
    # handlers in `scrape` anticipated: the exception went out past all of them,
    # nothing settled the row, and the attempt read 'running' for ever. On the
    # interface that is indistinguishable from a scrape still in flight, so a
    # bucket name nobody could write to looked like a slow page.
    from screener.blobs import BlobStore, BlobWriteFailed
    from screener.magpie import robots
    from screener.magpie.config import MagpieConfig
    from screener.magpie.run import scrape

    class Full(BlobStore):
        def put(self, path: str, data: bytes) -> None:
            raise BlobWriteFailed(f"PUT {path} returned 400 InvalidBucketName")

        def get(self, path: str) -> bytes:
            raise AssertionError("nothing was written to read back")

    page = "<html><head><title>T</title></head><body><article><p>{}</p></article></body></html>".format(
        " ".join(f"word{i}" for i in range(200))
    )

    def site(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        return httpx.Response(200, text=page)

    robots.forget()
    # A literal public address, so the reachability guard needs no DNS. Nothing
    # is ever sent to it: the transport answers every request.
    outcome = scrape(
        "http://93.184.216.34/a",
        config=MagpieConfig(strategies=("direct",), host_delay=0.0),
        conn=fresh_db,
        blobs=Full(),
        transport=httpx.MockTransport(site),
    )
    robots.forget()

    assert outcome.document is None
    assert outcome.refused is not None
    # Not `all_strategies_failed`: the route worked. Saying otherwise sends the
    # next person to look at the site when the fault is ours.
    assert outcome.refused.reason == "not_stored"

    with fresh_db.cursor() as cur:
        cur.execute("select state, reason, error from magpie.attempt order by id desc limit 1")
        state, reason, error = cur.fetchone()
    assert (state, reason) == ("failed", "not_stored")
    assert "InvalidBucketName" in error
