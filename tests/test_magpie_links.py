"""The sites a document points at.

The decision this file defends is which links count. Every anchor on a page
includes the site's own furniture, and a sources list full of "Donate" and
"Privacy Policy" is worse than no sources list: it looks like it worked.
"""

import httpx
import pytest

from screener.magpie.links import MAX_ANCHOR, references

BODY = " ".join(f"word{i}" for i in range(120))

# A page shaped like a real one: navigation and a footer that link away, and an
# article that cites two sources. The furniture outnumbers the citations, which
# is the case that decides where links are read from.
PAGE = f"""<html><head><title>A piece about markets</title></head><body>
<nav>
  <a href="https://donate.example.org/give">Donate</a>
  <a href="https://policy.example.org/privacy">Privacy Policy</a>
  <a href="https://fr.example.org/wiki/X">Français</a>
</nav>
<article>
  <h1>A piece about markets</h1>
  <p>{BODY} and see <a href="https://sec.gov/rules/market-order">Market Order</a>
     for the rule itself.</p>
  <p>{BODY} as <a href="https://doi.org/10.1000/abc">"A Study of Spreads"</a> found,
     and again in <a href="https://doi.org/10.1000/abc">the same study</a>.</p>
  <p>{BODY} on <a href="/local/page">our own site</a> as well.</p>
</article>
<footer><a href="https://donate.example.org/give">Donate</a></footer>
</body></html>"""

HERE = "https://news.example.com/piece"


def test_the_links_are_the_articles_own_and_not_the_sites_furniture():
    # The decision this whole module rests on. Walking every anchor on a real
    # Wikipedia page returns two hundred and forty eight external links whose
    # most frequent are Donate, Privacy Policy and the language list. Taking
    # them from the extracted body instead leaves the citations.
    hosts = {r.host for r in references(PAGE, HERE)}
    assert "sec.gov" in hosts
    assert "doi.org" in hosts
    assert "donate.example.org" not in hosts
    assert "policy.example.org" not in hosts
    assert "fr.example.org" not in hosts


def test_a_link_to_the_same_site_is_not_a_source():
    # A page linking to its own site is navigation however deep in the article
    # it sits. What is wanted is where it sends you away to.
    assert all(r.host != "news.example.com" for r in references(PAGE, HERE))


def test_one_source_cited_twice_is_one_link_with_a_count():
    found = {r.url: r for r in references(PAGE, HERE)}
    doi = next(r for u, r in found.items() if "doi.org" in u)
    assert doi.occurrences == 2


def test_the_links_own_words_are_kept_as_the_description():
    # The cheapest honest answer to "what is this": the article already
    # described it, and better than anything a fetch would return.
    found = {r.host: r for r in references(PAGE, HERE)}
    assert found["sec.gov"].anchor == "Market Order"
    assert "A Study of Spreads" in found["doi.org"].anchor


def test_the_first_mention_names_a_source_not_the_later_ones():
    # Second and later mentions are usually "ibid", "the same study" or the
    # bare "Archived" that follows an archive link.
    found = {r.host: r for r in references(PAGE, HERE)}
    assert found["doi.org"].anchor != "the same study"


def test_tracking_parameters_do_not_make_two_sources_of_one():
    page = PAGE.replace(
        '<a href="https://doi.org/10.1000/abc">the same study</a>',
        '<a href="https://doi.org/10.1000/abc?utm_source=newsletter">the same study</a>',
    )
    dois = [r for r in references(page, HERE) if r.host == "doi.org"]
    assert len(dois) == 1 and dois[0].occurrences == 2


def test_a_file_is_not_a_source():
    page = PAGE.replace(
        '<a href="https://sec.gov/rules/market-order">Market Order</a>',
        '<a href="https://sec.gov/rules/market-order.pdf">Market Order</a>',
    )
    assert all(r.host != "sec.gov" for r in references(page, HERE))


def test_a_relative_citation_is_resolved_against_the_page():
    # A citation is as likely to be written relative as absolute.
    page = PAGE.replace('href="https://sec.gov/rules/market-order"', 'href="//sec.gov/rules/x"')
    assert any(r.host == "sec.gov" for r in references(page, HERE))


def test_a_very_long_citation_is_cut_rather_than_stored_whole():
    page = PAGE.replace(">Market Order<", ">" + "t" * (MAX_ANCHOR + 200) + "<")
    found = {r.host: r for r in references(page, HERE)}
    assert len(found["sec.gov"].anchor) == MAX_ANCHOR


def test_a_page_that_will_not_parse_has_no_sources_rather_than_failing():
    # The same answer as a page that cites nothing, and neither is worth
    # failing a page view over.
    assert references("\x00\x01 not markup", HERE) == []


def test_a_page_with_no_article_has_no_sources():
    assert references("<html><body><nav><a href='https://x.com/'>x</a></nav></body></html>", HERE) == []


def test_reading_links_opens_no_socket(monkeypatch):
    # The property the whole feature depends on: the page is already in the
    # blob store, so having the sources costs no request. trafilatura ships a
    # downloader that would work perfectly well and route around both the proxy
    # ladder and the daily meter.
    def explode(*args, **kwargs):
        raise AssertionError("reading links opened a socket")

    for target in ("get", "post", "request"):
        monkeypatch.setattr(httpx, target, explode, raising=False)
    monkeypatch.setattr(httpx.Client, "send", explode)
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", explode)

    assert len(references(PAGE, HERE)) == 2


@pytest.mark.parametrize("scheme", ["javascript:alert(1)", "mailto:a@b.com", "data:text/html,x"])
def test_something_that_is_not_a_web_address_is_not_a_source(scheme):
    page = PAGE.replace('href="https://sec.gov/rules/market-order"', f'href="{scheme}"')
    assert all("sec.gov" != r.host for r in references(page, HERE))
