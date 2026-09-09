"""What counts as the same page.

A URL is a poor identity and this is where that is fixed. The failure these
defend against is one article becoming three documents because it arrived from
an email, a tweet and a search result.
"""

from screener.magpie import canonical, host_of, is_http, looks_binary


def test_tracking_parameters_are_stripped_so_one_article_is_one_row():
    # The same piece shared from three places must be one document. Keying on
    # the raw string is `screener.universe`'s "match on symbol" mistake in a
    # different costume: one thing reads as several.
    email = "https://example.com/story?utm_source=newsletter&utm_medium=email"
    tweet = "https://example.com/story?fbclid=abc123"
    plain = "https://example.com/story"
    assert canonical(email) == canonical(tweet) == canonical(plain)


def test_a_whole_advertising_family_is_stripped_not_just_the_famous_one():
    # From a real link. An earlier version caught `gclid` and `gbraid` and let
    # `gad_source`, `gad_campaignid` and `gclsrc` through, so one landing page
    # reached from two campaigns would have been two documents.
    ad = (
        "https://subs.ft.com/H2Sale2026?gclsrc=aw.ds&gad_source=1"
        "&gad_campaignid=2377&gbraid=0AAA&gclid=EAIa"
    )
    assert canonical(ad) == "https://subs.ft.com/H2Sale2026"


def test_a_meaningful_query_parameter_survives():
    # Stripping everything would collapse genuinely different pages. `?id=7` is
    # the page, `?utm_source=x` is how you got to it.
    assert "id=7" in canonical("https://example.com/a?id=7&utm_source=x")


def test_a_fragment_is_not_part_of_the_identity_of_a_page():
    assert canonical("https://example.com/a#section-3") == canonical("https://example.com/a")


def test_host_casing_and_a_default_port_do_not_make_a_second_document():
    assert canonical("https://WWW.Example.com:443/a") == canonical("https://www.example.com/a")


def test_a_port_that_is_not_the_default_is_kept():
    # Two services on one host are two hosts as far as a document is concerned.
    assert ":8443" in canonical("https://example.com:8443/a")


def test_parameters_are_ordered_so_the_key_does_not_depend_on_typing_order():
    assert canonical("https://e.com/a?b=2&a=1") == canonical("https://e.com/a?a=1&b=2")


def test_a_bare_host_and_a_trailing_slash_agree():
    assert canonical("https://example.com") == canonical("https://example.com/")


def test_a_file_is_recognised_before_anything_is_fetched():
    # `FetchResult` carries text and no Content-Type, so a PDF arrives as
    # mojibake rather than an error: the extractor finds nothing, the word floor
    # fires, and the ladder escalates to the billed rung to fail again. Refusing
    # by path is the cheap way not to pay for that.
    assert looks_binary("https://example.com/report.pdf")
    assert looks_binary("https://example.com/a/b/slides.PPTX")
    assert not looks_binary("https://example.com/story-about-pdf-readers")


def test_only_http_addresses_are_pages():
    assert is_http("https://example.com/a")
    assert not is_http("file:///etc/passwd")
    assert not is_http("javascript:alert(1)")
    assert not is_http("not a url at all")


def test_the_host_is_read_back_lower_cased():
    assert host_of("https://WWW.Example.COM/a") == "www.example.com"
