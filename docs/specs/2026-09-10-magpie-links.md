# Magpie: the sites a document points at

*2026-09-10*

## The problem

An article is mostly a set of references to other places, and Magpie kept the
article while throwing those away. Opening a document should show the sites it
cites, and give one click to keep any of them.

## Decisions

**D1. The links are the article's, not the site's.** Walking every `<a href>` on
the *History of Google* page returns 248 external links whose most frequent are
*Donate*, *Privacy Policy*, *Edit links* and the interwiki language list.
trafilatura's extracted `body` is the same tree the text comes from, with the
navigation and footer already removed, and its `ref` elements are the links the
article itself makes: 214 real citations across 99 sites, and none of the
furniture. The boilerplate removal that earns the library its place earns it
twice.

**D2. Reading them opens no socket.** The page is already gzipped in the blob
store and `blob_path` is on every document row. **This is the first thing in the
project to read a payload back** — until now they were written as evidence and
never opened. Done once per document, on first view, stamped by `links_read_at`,
so a document nobody opens is never read and opening one twice costs nothing.

**D3. One table, and the sites are a `group by`.** A `magpie.site` table would
hold no fact that is not derivable from `magpie.link`. When per-site metadata is
actually fetched, that is when it earns a table.

**D4. `scraped_id` is the frontier.** `where scraped_id is null` is exactly the
queue a crawler would drain. It exists before anything drains it so that cycle
arrives as a new process rather than a migration over live rows, which is the
argument `magpie.attempt`'s state column already makes.

**D5. Following a link is the ordinary scrape path.** robots, the ladder, the
daily meter over the billed rung. One link, once, because somebody clicked it.

**D6. Links cascade with their document; a followed document does not.**
Deleting an article deletes the reading of it, because a reading is not evidence
of anything once the page is gone — unlike `magpie.attempt`, which records that
money was spent and survives. Deleting what a link *produced* leaves the link,
unfollowed, rather than deleting the reference to it.

## Amending the previous spec

`docs/specs/2026-09-09-magpie-scraping.md` says under *What this deliberately
does not do*: **"No crawling. One URL in, one document out. A frontier, a link
graph and a politeness budget are a cycle of their own."**

Half of that is now false and the entry is replaced rather than left to argue
with the code. The link graph exists and the frontier exists. What still does
not exist is anything that fetches without being asked: no queue is drained, no
depth is followed, and every request is still one somebody clicked. The
politeness budget remains untouched because nothing yet makes enough requests
to need one.

## What this deliberately does not do

- **It still does not crawl.** The frontier exists; nothing drains it.
- **No per-site metadata fetching.** A description per site is one request per
  site, which on one article is ninety-nine. The anchor text is what the article
  itself called it, costs nothing, and is usually better.
- **No link graph across documents.** `magpie.link` is per document; whether two
  articles cite the same source is a query, not a table.
- **No new Steven tool.** `sql` reaches this through the grant.
- **No recursion and no "follow all".** One click, one link.

## Cost

Nothing. No new image, no new service, no request. The whole feature is a parse
of bytes that were already kept.
