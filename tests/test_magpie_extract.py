"""Turning a page into an article, and the line that keeps it offline.

The failure worth a whole test file is quiet: trafilatura ships a downloader
that works perfectly well, and using it would bypass the proxy ladder and the
daily meter at once. The article would arrive, the cost would go unrecorded,
and the strategy column would be a lie.
"""

import httpx
import pytest

from screener.magpie.extract import MIN_WORDS, NotAnArticle, extract

BODY = " ".join(f"word{i}" for i in range(120))

PAGE = f"""<html><head>
<title>Site name — A headline about semiconductors</title>
<link rel="canonical" href="https://example.com/real-address"/>
<meta property="article:published_time" content="2026-09-01T10:00:00Z"/>
<meta name="author" content="A Reporter"/>
</head><body>
<nav>Home Business Markets</nav>
<article><h1>A headline about semiconductors</h1><p>{BODY}</p></article>
<footer>Copyright, cookie notice, related articles</footer>
</body></html>"""


def test_the_article_comes_out_and_the_furniture_does_not():
    article = extract(PAGE, url="https://example.com/asked-for")
    assert "semiconductors" in article.title
    assert "word7" in article.text
    # The navigation and the footer are exactly what a library is for.
    assert "cookie notice" not in article.text
    assert "Home Business Markets" not in article.text


def test_the_byline_and_the_date_are_read_from_the_page():
    article = extract(PAGE, url="https://example.com/asked-for")
    assert article.author == "A Reporter"
    assert article.published is not None and article.published.year == 2026


def test_the_canonical_link_in_the_page_beats_the_url_that_was_requested():
    # A syndicated copy, an AMP variant and a tracking link all point at one
    # address the publisher names. Kept beside what we asked for, not instead.
    article = extract(PAGE, url="https://example.com/asked-for?utm_source=x")
    assert article.canonical_url == "https://example.com/real-address"


def test_a_challenge_page_is_not_an_article():
    # An interstitial is several kilobytes of markup and a sentence of text, so
    # a byte-length floor never fires. The word count is the signal, and it is
    # the same signal for a cookie wall and a JavaScript-only shell.
    challenge = "<html><body><h1>Just a moment…</h1><p>Checking your browser.</p></body></html>"
    with pytest.raises(NotAnArticle):
        extract(challenge)


def test_a_page_just_under_the_floor_is_refused_and_just_over_is_kept():
    short = " ".join(["word"] * (MIN_WORDS - 5))
    long = " ".join(["word"] * (MIN_WORDS + 40))
    with pytest.raises(NotAnArticle):
        extract(f"<html><body><article><p>{short}</p></article></body></html>")
    assert extract(f"<html><body><article><p>{long}</p></article></body></html>").text


def test_rubbish_is_refused_rather_than_raising_whatever_the_parser_raises():
    # A page that will not parse is an ordinary outcome, not an incident, and
    # the caller should get one exception type rather than lxml's.
    with pytest.raises(NotAnArticle):
        extract("\x00\x01\x02 not markup at all")


def test_extraction_makes_no_network_call_of_its_own(monkeypatch):
    # The line this file exists for. trafilatura's own `fetch_url` would work,
    # and would silently route around the proxy ladder and the daily meter.
    def explode(*args, **kwargs):
        raise AssertionError("extraction opened a socket")

    for target in ("get", "request", "post"):
        monkeypatch.setattr(httpx, target, explode, raising=False)
    monkeypatch.setattr(httpx.Client, "send", explode)

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    article = extract(PAGE, url="https://example.com/asked-for")
    assert article.word_count > MIN_WORDS


def test_the_word_count_is_the_article_and_not_the_markup():
    article = extract(PAGE, url="https://example.com/a")
    assert article.word_count == len(article.text.split())
    assert article.word_count < len(PAGE.split())
