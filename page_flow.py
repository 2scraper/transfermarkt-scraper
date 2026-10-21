"""
page_flow.py
------------
Transfermarkt's page-state policy, shared by all three engines.

Four modes, and they do not all paginate the same way — this is the module
that says so in one place instead of three:

    market-values   a ranking table, paginated, but the pagination is NOT a
                     plain query parameter — see "The gallery pagination
                     trap" below. Sequential only.
    club-squad      one club's roster. A single page; there is no page 2.
    player          one profile page. A single page.
    transfers       a ranking-shaped table, paginated with a plain and
                     independently-addressable `?page=N` — the one mode
                     here that supports --concurrency.

Why this module exists at all, when part of this family keeps each engine
self-contained: a page here can answer four different ways and three of them
want a different response.

    content    parse it
    blocked    a network- or vendor-level refusal before any content arrived
    captcha    a challenge widget — the paid path
    empty      a real page with nothing to parse on it — an out-of-range
               page number, or a squad URL for a club with no current season
               data — report it as such, do not retry it and do not go
               looking for a proxy problem

Three copies of that triage would drift, exactly as this family's other
members found; the same reasoning keeps the exit-code mapping in
`output_writer.finish_run()`.

The gallery pagination trap — and correcting it
-------------------------------------------------
First measured 2026-09-10 against
`/spieler-statistik/wertvollstespieler/marktwertetop`, and originally
written up here as THE shape of this mode's pagination:

  * The page's own `<link rel="next">` and its "2" pagination link both
    carry a DOUBLED path segment — `.../galerie/{uuid}/page/2//page/2` —
    which 404s verbatim.
  * Fixing the doubled segment by hand (`.../galerie/{uuid}/page/2`, single
    occurrence) does NOT fix the problem either: fetched as a fresh,
    cookie-less request, it returns PAGE 1's content again, not page 2.

That capture was real, and is kept as a fixture (see smoke_test.py) — this
is not a retraction of the observation. What WAS wrong is what this module
concluded from ONE capture: that this is the deterministic shape of every
market-values request, and that `pagination_is_addressable()` should
therefore hardcode `False` for this mode unconditionally, the one mode this
file special-cased out of the "verify first" check every other paginated
mode gets.

**Re-measured 2026-09-10 in a wider session: 28 consecutive fresh fetches of
the SAME URL all returned the plain, undoubled `?page=N` shape — identical
in form to `/statistik/neuestetransfers` — and only the original single
capture showed the `galerie/{uuid}` shape.** So the gallery shape is real
but INTERMITTENT (roughly 1 in ~29 in that sample — treat that ratio as
indicative, not precise; nothing here explains WHY the site serves one
shape or the other, only that it does), not the page's normal behavior.
And critically: in the plain-shape case, a stateless fetch of the
CONSTRUCTED `?page=2` genuinely works — measured 0 shared player ids
against page 1, 25 rows each, exactly the same independence
`/statistik/neuestetransfers` has always had.

So this mode does not need — and, as originally written, wrongly refused —
the SAME "verify first" mechanism `transfers` already used:
`pagination_is_addressable()` now runs the identical check for every
paginated mode, no special case. When page 1's own next-link agrees with
what `page_url()` would build (the common case, per the re-measurement
above), this mode gets the same fast, independently-addressable path
`transfers` does. When it does not agree (the rare gallery case), the
function correctly returns False and the caller falls back to chaining the
site's own link one page at a time within a single session — the ORIGINAL
safe behavior, now used only when it is actually needed rather than on
every run. Nothing about the fallback path changed; only the gate deciding
when to use it did.

**Re-measured 2026-10-21: the gallery shape is not rare, and chaining it
does not work.** It appeared on 3 of 8 market-values runs (local Playwright
and the Scraping Browser alike), and the chained link — `…/page/10//page/2`
— answered 404 every time, so each of those runs ended at page 1 with exit
6. In the same sessions the plain `?page=2` and `?page=3` served the real
pages (25 rows each, 0 players shared with page 1 or each other, 3 of 3
gallery sessions). So for market-values the gallery link is now ignored and
`?page=N` planned; `PageSequence` still reports a page that repeats.

`--concurrency` remains `transfers`-only regardless (see
`CONCURRENCY_CAPABLE_MODES` in each engine) — not because market-values
pagination is unaddressable (it usually is, per above), but because this
project has not tested N parallel workers each independently hitting a page
that intermittently switches shape. The planned-vs-chained decision above
is safe to make per run because it happens once, from page 1's own answer;
running several workers concurrently against a mode that can silently
change shape mid-run is a different, untested risk this repo is not taking
on speculatively. Revisit if a live run's data supports it.

`/statistik/neuestetransfers` was and remains simpler: its `<link
rel="next">` reads `?page=2`, a bare query parameter, and a stateless fetch
of `?page=2` measured 0 shared transfer ids against page 1 (25 rows each) —
genuinely, independently addressable on every capture taken of it so far,
which is the one mode `--concurrency` is enabled for. Its END is not a
missing page, though: a page number past the end serves the last page again
(measured 2026-10-15), which `PageSequence` below tells apart from a fresh
page.

What this module deliberately does NOT contain
------------------------------------------------
No scrolling, no lazy-load hydration. Every page captured for this repo
(a ranking page, a squad page, a transfer list, a player profile) arrived
as complete server-rendered HTML with every row already in it — there is no
JSON-LD anywhere on this site (checked on all of the above; the only
structured data present is a `schema.org/Person` microdata block on a
player's own profile header, read directly rather than through a script
tag), but there is also nothing to wait for that a DOM-ready check does not
already cover.

The functions here are either pure or driven through small callables, so
each engine passes its own driver's primitives — see the sibling repos'
`page_flow.py` docstrings for why no JavaScript crosses this boundary.
"""

import logging
from typing import Callable, Dict, Optional

from product_parser import detect_page_state, page_url

logger = logging.getLogger("page_flow")


# What "the page has painted" means, per mode. All four modes are
# server-rendered in full on first response (measured — see module
# docstring), so these anchors exist to confirm the right table/header
# arrived, not to wait out a hydration step.
READY_SELECTORS = {
    "market-values": "table.items tbody tr",
    "club-squad": "table.items tbody tr",
    "transfers": "table.items tbody tr",
    "player": "header.data-header",
}

# How many rows must appear before a LISTING counts as loaded rather than as
# a lucky single match — must be > 1, per the family rule, so a wait cannot
# resolve on one unrelated row. 3 against a measured floor of 25 (transfers)
# and 30 (market-values, one ranking page) leaves room for a short final
# page without waiting out the timeout on it. club-squad has no fixed floor
# (a small club's extended squad can be under a dozen), so it also uses 3.
MIN_ROW_MATCHES = {"market-values": 3, "club-squad": 3, "transfers": 3, "player": 0}

CONTENT_TIMEOUT_MS = {"market-values": 20000, "club-squad": 15000,
                      "transfers": 20000, "player": 15000}


def ready_selector(mode: str) -> str:
    return READY_SELECTORS.get(mode, READY_SELECTORS["market-values"])


def min_matches(mode: str) -> int:
    return MIN_ROW_MATCHES.get(mode, 3)


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS.get(mode, 20000)


def ready_count(mode: str) -> int:
    """How many matches the readiness wait must SEE before it stops.

    `min_matches` is a FLOOR a page must clear -- "more than three rows", not
    "three" -- which is what makes it immune to resolving on one unrelated
    row. `wait_for_count` below is written the other way round, waiting until
    it has seen at least `minimum`, because that is the shape the rest of the
    family uses. This converts between the two in ONE place rather than
    leaving a `+ 1` in each of the three engines, where the first one to lose
    it would wait on a different threshold than the other two and nothing
    offline would notice.
    """
    return min_matches(mode) + 1


def wait_for_count(count: Callable[[str], int], sleep: Callable[[int], None],
                   selector: str, minimum: int, timeout_ms: int,
                   poll_ms: int = 250) -> int:
    """Poll `selector` until `minimum` elements match, or the budget runs out.

    Polls a COUNT rather than waiting on an evaluated string. Playwright's
    `wait_for_function` and pyppeteer's `waitForFunction` both hand the
    browser a STRING to evaluate, which a site whose CSP lacks `unsafe-eval`
    refuses outright -- on a sibling repo (tokopedia-scraper) that was an
    `EvalError` and exit 1 on the site's most obvious URL, reproducing on one
    of its two listing routes and not the other, because the two are served
    by different renderers with different headers.

    Transfermarkt's own CSP was not the reason this was changed and is not
    being worked around here; this is the eval simply not being invited back
    in when a header changes. `querySelectorAll` through the protocol is a
    CDP call, works under any CSP, and spells the same in all three drivers
    -- which is why all three `_driver()` adapters already expose `count`.

    `count` is expected to swallow its own driver errors and return 0: this
    polls a page that may be navigating under it, and an exception from the
    500th millisecond of a 20-second wait should read as "nothing there yet",
    not take the run down.

    Returns the last count seen, so a caller can tell "painted" from "timed
    out with three of them".
    """
    waited = 0
    seen = count(selector)
    while seen < minimum and waited < timeout_ms:
        sleep(poll_ms)
        waited += poll_ms
        seen = count(selector)
    if seen < minimum:
        logger.info("readiness wait ended at %d/%d matches for %s after %dms",
                    seen, minimum, selector, waited)
    return seen


# A page holding less than this fraction of page 1's own row count is logged
# as thin -- INFORMATIONAL ONLY, never a retry trigger and never a reason to
# change the exit code. A short final page is a correct answer (measured:
# both paginated listings return exactly 25 rows per full page, so a last
# page of a handful is expected, not a fault) -- what this catches is the
# case this family has met before: a page beyond the site's
# real depth, or a markup regression, coming back with only a few rows
# instead of a clean "no new products" the sku-freshness check would have
# caught outright. This repo deliberately does NOT add a numeric PAGE_CAP
# (as catawiki-scraper does) on top of this: the sku-based "no new rows"
# stop condition already terminates on DATA rather than a guessed page
# count, which is the more trustworthy stop -- a
# hardcoded cap here would be redundant insurance for a failure mode this
# repo's fresher, sku-driven check already covers.
THIN_PAGE_RATIO = 0.4


def is_thin_page(row_count: int, first_page_count: int) -> bool:
    if first_page_count <= 0:
        return False
    return row_count < first_page_count * THIN_PAGE_RATIO


# A market value is published for almost every row on market-values/
# club-squad (only a free agent or a very old veteran prints "-"), so
# coverage below this floor across a page is a parsing regression to flag,
# not the site's own doing -- matching the family's own "DOM-confirmation
# coverage... warning below 90%" convention, loosened slightly
# here since there is no second source to reconcile against on this site (see
# product_parser.py's docstring) and a genuinely elderly/reserve-heavy squad
# page can legitimately run a little under 100%. Deliberately NOT applied to
# --mode transfers: a fee is legitimately absent for a loan, a free transfer
# or an undisclosed fee (see output_writer.Transfer.fee_type), so low price
# coverage there is normal, not a signal.
PRICE_COVERAGE_FLOOR = 0.85


def classify(html: Optional[str], *, status: Optional[int] = None,
             url: Optional[str] = None,
             headers: Optional[Dict[str, str]] = None) -> str:
    """The page's state, as the engines see it. A thin wrapper over
    product_parser.detect_page_state, for the same reason the sibling repos
    keep one: every engine reaches the policy through one name.

    `status` and `headers` are both OPTIONAL because not every engine can
    supply them, and the classifier has to stay correct for the ones that
    cannot. Playwright exposes the response object, and the Scraper API
    returns `{"status", "headers", "body"}` — those two can pass both.
    Selenium and pyppeteer read the rendered DOM through the driver and have
    no response object to ask, so they pass neither, and for them the vendor
    markers are the ONLY thing standing between an AWS WAF captcha and a run
    that reports it as an empty listing. That asymmetry is why A1's markers,
    not this header, are the load-bearing half of the fix.

    Keyword-only after `html` on purpose: `classify(html, status, url)` with
    `status` positional is the exact signature that crashed two of three
    engines in a sibling repo on their first fetch, because a
    caller wrote `classify(html, url=...)` and bound `url` to `status`.
    """
    return detect_page_state(html or "", status=status, url=url, headers=headers)


# What each state means for the run. Kept as data, not as three copies of an
# if-chain, so that an engine cannot quietly
# disagree with its twins about whether a page is worth retrying or paying
# for.
# AWS WAF's CHALLENGE action (challenge.js, no widget) is a script a real
# browser runs by itself: it computes a token, sets `aws-waf-token` and
# reloads the page. Measured 2026-09-24 from a datacentre VPS on the
# market-values page: 3 of 3 fresh headless Chromium sessions cleared it in
# 1.0 s and were served the real page. Every engine used to report it as
# blocked 0.4 s after the fetch instead, which made every run from a
# datacentre address or a GitHub runner exit 3. 15 s is fifteen times the
# measured figure; a challenge still standing after that is reported as
# before.
AWS_CHALLENGE_SETTLE_MS = 15000
AWS_CHALLENGE_POLL_MS = 500


def wait_for_waf_challenge(content: Callable[[], Optional[str]],
                           sleep: Callable[[int], None],
                           still_challenged: Callable[[str], bool]) -> bool:
    """Poll until the WAF's own script has replaced its page. True if it did.

    `content` and `sleep` are the engine's driver operations and
    `still_challenged(html)` is the detector, so no driver dialect crosses
    this boundary.
    """
    waited = 0
    while waited < AWS_CHALLENGE_SETTLE_MS:
        sleep(AWS_CHALLENGE_POLL_MS)
        waited += AWS_CHALLENGE_POLL_MS
        html = content()
        if html and not still_challenged(html):
            return True
    return False


STATE_POLICY = {
    "content": {"retry": False, "solve": False, "blocked": False},
    "blocked": {"retry": True, "solve": False, "blocked": True},
    "captcha": {"retry": True, "solve": True, "blocked": True},
    # NOT retried: an out-of-range page or an empty squad URL is a correct
    # answer to the question that was asked.
    "empty": {"retry": False, "solve": False, "blocked": False},
    # The site's own "ERROR 404" page, or an HTTP 404/410: the address names
    # nothing (a club id that does not exist, a mistyped --url). Not a
    # refusal, so it is neither retried from another exit nor paid for, and
    # all four engines reach the same answer whether or not they can see the
    # status code.
    "not_found": {"retry": False, "solve": False, "blocked": False},
    # The target's own 5xx: an HTTP 5xx status, or the edge's bare nginx 5xx
    # page when no status is visible. A transient server error, not a
    # refusal: never rotated past (`retry` here means "from another exit"),
    # never paid for, never counted as blocked. Retried on the same exit by
    # PageAttempts below, then the page is failed as STOP_TARGET_HTTP_5XX.
    "server_error": {"retry": False, "solve": False, "blocked": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("retry", False)


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("solve", False)


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("blocked", False)


# Stop reason for a run whose first page was the site's "not found" answer.
# A complete answer (exit 4 with no rows): the site said the thing asked for
# does not exist, which is a fact about the site, not a failed fetch.
STOP_NOT_FOUND = "not_found"

# Stop reason for a page the target answered with a 5xx after every retry.
# Not complete and not blocked: exit 5 on page 1, exit 6 on a later page.
STOP_TARGET_HTTP_5XX = "target_http_5xx"

# How many times a 5xx page is fetched again on the same exit before it is
# failed. Measured 2026-10-15: the edge answered 3 of about 30 page loads in
# one evening with a 155-byte nginx 502, each time for a single page, and a
# later fetch of the same URL was served. Backoff: --retry-delay x 1, x 2.
SERVER_ERROR_RETRIES = 2


class PageAttempts:
    """The block-retry loop's iterator, plus a bounded same-exit retry for a
    server error.

    Yields block-attempt indices 0..block_retries like `range()`. After
    `again_after_server_error()` returns True, the NEXT step yields the same
    index again, so a 5xx retry does not use up a proxy rotation and never
    triggers one. The page's SolveBudget lives outside this loop and is not
    reset by it.
    """

    def __init__(self, block_retries: int, retry_delay: float = 0.0,
                 server_retries: Optional[int] = None,
                 sleep: Optional[Callable[[float], None]] = None):
        self.block_retries = block_retries
        self.retry_delay = retry_delay
        self.server_retries = (SERVER_ERROR_RETRIES if server_retries is None
                               else server_retries)
        self.server_retries_used = 0
        self._sleep = sleep
        self._index = 0
        self._repeat = False

    def __iter__(self):
        while self._index <= self.block_retries:
            self._repeat = False
            yield self._index
            if not self._repeat:
                self._index += 1

    def again_after_server_error(self, page_num: int, status=None) -> bool:
        if self.server_retries_used >= self.server_retries:
            return False
        self.server_retries_used += 1
        pause = self.retry_delay * (2 ** (self.server_retries_used - 1))
        logger.warning("Page %d: the site answered with a server error (HTTP "
                       "%s) — fetching it again in %.1fs (%d/%d).", page_num,
                       status if status is not None else "5xx, read from the page",
                       pause, self.server_retries_used, self.server_retries)
        if pause:
            (self._sleep or __import__("time").sleep)(pause)
        self._repeat = True
        return True


# Stop reason for a served page that holds rows the parser could not read: a
# markup change, never an empty listing. Not complete, so exit 5 on page 1
# (nothing obtained) and exit 6 on a later page (rows kept, run incomplete).
STOP_PARSER_FOUND_NOTHING = "parser_found_nothing"


def redirected_off_entity(mode: str, requested_url: str,
                          final_url: Optional[str]) -> bool:
    """Whether a single-entity page answered by sending us somewhere else.

    Measured 2026-10-15: a player id that does not exist
    (`/x/profil/spieler/999999999`) is answered with HTTP 200 and a redirect
    to the market-values ranking, not a 404. Read as a player page it parses
    to nothing and used to be reported as an empty result. A profile or squad
    URL whose id is gone from the final URL is the site's not-found answer.
    """
    from product_parser import club_id_from_url, player_id_from_url
    if not final_url:
        return False
    if mode == "player":
        wanted = player_id_from_url(requested_url)
        return bool(wanted) and player_id_from_url(final_url) != wanted
    if mode == "club-squad":
        wanted = club_id_from_url(requested_url)
        return bool(wanted) and club_id_from_url(final_url) != wanted
    return False


def failure_reason(outcome) -> str:
    """The stop reason a failed page outcome gives the run. One place, so the
    four engines and their concurrent paths cannot name the same failure
    differently."""
    if getattr(outcome, "load_failed", False):
        return "page_load_timeout"
    if getattr(outcome, "parse_failed", False):
        return STOP_PARSER_FOUND_NOTHING
    if getattr(outcome, "server_error", False):
        return STOP_TARGET_HTTP_5XX
    if getattr(outcome, "state", None) == "not_found":
        # Only reachable on a page after the first: page 1's "not found" is
        # the run's answer (STOP_NOT_FOUND), a later page's is a page lost.
        return "page_not_found"
    return f"blocked_{outcome.blocked_by}"


# ---------------------------------------------------------------------------
# A later page that serves an earlier page again
# ---------------------------------------------------------------------------
# The listing has ended: the page before the repeat carried no next-page
# link, and the requested page served rows already seen. Complete.
STOP_LISTING_ENDED = "listing_ended"
# The page before carried a next-page link, yet the requested page served
# rows already seen: pages were requested and not served. Not complete.
STOP_PAGE_REPEATED = "page_repeated"


class PageSequence:
    """Which keys each served listing page carried, in page order.

    Measured 2026-10-15 on `/transfers/neuestetransfers/statistik`: the site
    answers a page number past the end with the LAST page again, HTTP 200,
    its pager still marking the last page as current (`?page=5`, `?page=401`
    and `?page=9999` all served page 4's two rows when the listing had four
    pages). The default `/statistik/neuestetransfers` view held 20 rows that
    day, with no pager, so `?page=2` served page 1 again. Treated as fresh
    pages, both runs ended "complete" with pages counted that the site never
    served.

    A page repeats when it has rows and every one of them has a key already
    seen on an earlier page. Whether that is the listing's end or a lost page
    depends on the page before it: no next-page link there means the site
    said the listing ends (`listing_ended`); a next-page link there means the
    site promised a page and served an old one (`page_repeated`). A page
    before it that was not served at all (a concurrent run where it failed)
    counts as a promise, the cautious reading.
    """

    def __init__(self):
        self._pages: Dict[int, tuple] = {}   # page_num -> (frozenset, next_url)
        self.repeat: Optional[dict] = None

    def add(self, page_num: int, rows, next_url: Optional[str]) -> Optional[dict]:
        """Record one SERVED page (in page order). Returns the verdict dict
        the first time a page repeats earlier ones, else None."""
        keys = [getattr(r, "sku", None) for r in rows]
        seen = set()
        for earlier, (earlier_keys, _next) in self._pages.items():
            if earlier < page_num:
                seen |= earlier_keys
        repeated = (self.repeat is None and page_num > 1 and keys
                    and all(k is not None and k in seen for k in keys))
        self._pages[page_num] = (frozenset(k for k in keys if k is not None),
                                 next_url)
        if not repeated:
            return None
        key_set = set(keys)
        same_as = next((p for p, (ks, _n) in sorted(self._pages.items())
                        if p < page_num and ks == key_set), None)
        # The site answers any number past the end with the LAST page, so a
        # page that repeats page J exactly, with nothing served between them,
        # puts the pages between past the end as well: the last real page is
        # J, and J's own next link says whether the listing ended there.
        # Measured 2026-10-21 under --concurrency 4: pages 1-4 served, 5
        # timed out, 6 = page 4. Read as "5 promised", that complete listing
        # came out partial (pages_failed [5], listing_pages 5).
        last = page_num - 1
        if same_as is not None and not any(p in self._pages
                                           for p in range(same_as + 1, page_num)):
            last = same_as
        before = self._pages.get(last)
        promised = before is None or before[1] is not None
        self.repeat = {
            "page": page_num,
            "same_as": same_as,
            "last": last,
            "stop_reason": STOP_PAGE_REPEATED if promised else STOP_LISTING_ENDED,
        }
        logger.info(
            "Page %d served %s again (%d row(s), none new)%s.", page_num,
            f"page {same_as}" if same_as else "rows from earlier pages",
            len(keys),
            " although the page before it linked to a next page — recording "
            "it as a page not served" if promised else
            "; the page before it had no next-page link, so the listing "
            "ends there")
        return self.repeat

    def counts(self, page_num: int) -> bool:
        """False for every page past the listing's last real page — the
        repeated one, any unserved page before it that the repeat shows was
        past the end too, and everything after: they are not pages of this
        listing and are not counted as completed (or as failed)."""
        return self.repeat is None or page_num <= self.repeat["last"]

    def meta(self) -> dict:
        """Sidecar keys describing the repeat, empty when there was none."""
        if self.repeat is None:
            return {}
        return {"repeated_page": self.repeat["page"],
                "repeat_of_page": self.repeat["same_as"],
                "listing_pages": self.repeat["last"]}


def listing_filter_meta(mode: str, url: Optional[str]) -> dict:
    """Sidecar keys naming the market-value floor a transfers run actually
    used: the one in its URL (`--min-value`, or a typed --url), or the site's
    own default view, whose floor is not in the URL (EUR 500,000 when
    measured 2026-10-15; the site sets it, so it is not asserted here)."""
    if mode != "transfers":
        return {}
    from product_parser import min_value_of
    floor = min_value_of(url)
    return {"min_market_value_eur": floor,
            "market_value_filter": "url" if floor is not None else "site_default"}


def settle_repeats(outcomes) -> PageSequence:
    """Run PageSequence over finished outcomes in page order — the concurrent
    path's version of the check the sequential loop makes as it goes. Pages
    that were not served are skipped; the page after one counts it as a
    promise (see PageSequence)."""
    seq = PageSequence()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        if oc.ok and oc.state != "not_found":
            if seq.add(oc.page_num, oc.rows, getattr(oc, "next_url", None)):
                break
    return seq


# At most one paid solve per page. A challenge that survives a solved token
# is not one this run can pass, and a second solve is a second charge for the
# same answer.
SOLVES_PER_PAGE = 1


class SolveBudget:
    """The paid-solve allowance for ONE page, shared by every solve call site.

    A page reaches the solver once per block-retry attempt, and with a proxy
    pool each attempt is a fresh exit: before this, one page could buy
    `--proxy-block-retries + 1` solves. One object per page, checked BEFORE
    each purchase and never reset by a retry or a rotation, is what makes the
    limit a limit. A fresh exit is a reason to fetch again, not a fresh
    allowance.
    """

    def __init__(self, limit: Optional[int] = None):
        self.limit = SOLVES_PER_PAGE if limit is None else limit
        self.spent = 0

    def allows(self) -> bool:
        return self.spent < self.limit

    def take(self) -> bool:
        """Claim one solve. False when the allowance is spent. Counted
        before the purchase, because a solver can charge for an answer and
        still report failure."""
        if not self.allows():
            logger.info("This page's paid-solve allowance (%d) is spent; not "
                        "buying another.", self.limit)
            return False
        self.spent += 1
        return True


def comparable(url: str) -> str:
    """`url` reduced to the parts that decide WHICH PAGE it addresses.

    Unlike the sibling repos, Transfermarkt's own links were not observed to
    carry tracking parameters worth stripping (none were seen across any of
    the captured pages), so this only drops the fragment and normalises
    query-parameter ORDER — a next-link and a constructed URL that agree on
    every parameter but its order must still compare equal.
    """
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    parts = urlparse(url)
    kept = parse_qsl(parts.query, keep_blank_values=True)
    return urlunparse(parts._replace(query=urlencode(sorted(kept)), fragment=""))


def _query_only(url: str) -> str:
    """`url` reduced to JUST its query parameters, order-independent.

    See pagination_is_addressable's "A site can rename its own listing path"
    note for why the PATH is deliberately excluded here even though
    `comparable()` (used elsewhere for exhausted-pagination checks) keeps it.
    """
    from urllib.parse import urlparse, parse_qsl, urlencode
    query = parse_qsl(urlparse(url).query, keep_blank_values=True)
    return urlencode(sorted(query))


def pagination_is_addressable(mode: str, page1_url: str,
                              site_next_url: Optional[str]) -> bool:
    """Whether page N can be fetched without first fetching page N-1.

    The SAME check for every paginated mode, including `market-values` as of
    the first fix here — see "Correcting the gallery pagination trap" in the
    module docstring for why this used to hardcode False for that mode and
    no longer does. True when page 1's own next-link agrees with what
    `page_url()` would build for page 2; a next-link that disagrees means
    the site is not on the plain `?page=N` path THIS run, and this returns
    False so the caller chains the site's own link one page at a time
    instead of silently repeating a page under the wrong label. `club-squad`
    and `player` have no page 2 to ask about and are not routed through this
    function.

    `mode` is kept in the signature for callers and for the case a THIRD
    pagination shape turns up on some other mode later, even though no mode
    currently branches on it here.

    A site can rename its own listing path without changing how pagination
    works
    ----------------------------------------------------------------------
    Measured live 2026-09-12, two days after the gallery-trap
    re-measurement above: `/statistik/neuestetransfers`'s own `<link
    rel="next">` had moved to a DIFFERENT PATH —
    `/transfers/neuestetransfers/statistik?page=2` — while the query string
    was still the plain `?page=2` this function expects. Fetching BOTH the
    old constructed URL and the new next-link agreed byte-for-byte (the same
    25 transfer ids, 0 overlap with page 1 either way), so the site had
    simply exposed a second path for the same listing, not changed the
    pagination contract. The ORIGINAL version of this function compared the
    FULL url (via `comparable()`, which keeps the path) and would have
    called this unaddressable, silently falling back to slow link-chaining
    for a mode that was, in fact, still perfectly addressable. So only the
    QUERY is compared below, never the path.

    The one thing still checked against the path is the gallery trap's own
    signature: the page number encoded IN the path (`.../page/8/page/3//
    page/2`) rather than passed as a `?page=N` query parameter. A next-link
    shaped like that is never trusted as equivalent to a constructed URL
    regardless of what its query string says (it usually has none at all).
    """
    if not site_next_url:
        return True
    from urllib.parse import urlsplit
    if "/page/" in urlsplit(site_next_url).path:
        # The gallery link itself is broken (`…/page/10//page/2` answers
        # 404), so chaining it ended every such run at page 1 with exit 6 —
        # 3 of 8 market-values runs on 2026-10-21, both transports. In the
        # same sessions the plain `?page=2` and `?page=3` served the real
        # pages (25 rows each, 0 shared with page 1 or each other, 3 of 3
        # gallery sessions), so for market-values the constructed URL is
        # used instead. PageSequence still catches a page that repeats.
        # Measured on market-values only; other modes keep refusing.
        if mode == "market-values":
            logger.info("page 1's next-link is the gallery shape (%s) — it "
                        "answers 404, so page 2+ are fetched as ?page=N.",
                        urlsplit(site_next_url).path[-40:])
            return True
        return False
    return _query_only(site_next_url) == _query_only(page_url(page1_url, 2))


# ---------------------------------------------------------------------------
# Why a --cdp-endpoint connection failed
# ---------------------------------------------------------------------------
def cdp_connect_hint(error_text: str) -> str:
    """The next step after a failed CDP connect, chosen by what the error
    says. One hint for every failure used to blame a busy profile even for a
    host that does not resolve (`getaddrinfo ENOTFOUND`, 2026-10-21).

    A 500 on the WebSocket upgrade has two causes that look identical from
    the driver, which shows only the status: the profile is held by another
    run (`profile_locked`) or the profile's own exit failed (`proxy_error`),
    both met on 2026-10-21."""
    text = (error_text or "").lower()
    if any(s in text for s in ("enotfound", "getaddrinfo", "name or service not known",
                               "nodename nor servname", "econnrefused",
                               "connection refused", "ehostunreach", "enetunreach")):
        return ("The endpoint's host could not be reached — check the host and "
                "port in --cdp-endpoint / TRANSFERMARKT_CDP_ENDPOINT and this "
                "machine's network. Nothing reached the Scraping Browser API.")
    if "401" in text or "unauthorized" in text:
        return ("The Scraping Browser API refused the login or password in the "
                "endpoint. Fetch a fresh one: python3 tools/browser_profile_client.py "
                "use --account-id N --write-env")
    if "500" in text:
        return ("A 500 on connect means the profile is busy — another run still "
                "holds this `pid` (one live connection per profile) — or the "
                "profile's exit failed (proxy_error). Wait for the other run, "
                "or use a different pid; if it persists, check the account's "
                "proxy with `tools/browser_profile_client.py accounts`.")
    return ("Nothing reached the target site. A Scraping Browser profile allows "
            "one live connection at a time, so another run on this `pid` is the "
            "first thing to rule out.")


# ---------------------------------------------------------------------------
# --retries
# ---------------------------------------------------------------------------
import argparse as _argparse  # noqa: E402  (kept beside its only user)


def attempts(value) -> int:
    """argparse `type=` for --retries. Rejects anything below 1.

    WHY THIS IS A USAGE ERROR AND NOT A CLAMP. The page-load loop is
    `for attempt in range(1, retries + 1)`, so `--retries 0` makes its body
    unreachable: nothing is fetched, and the engine then judges whatever the
    browser holds — `about:blank`, 39 bytes of
    `<html><head></head><body></body></html>` — and reports a block or an
    empty result in a few dozen milliseconds. That is a confident, fast,
    plausible answer to a question nobody asked.

    Measured on 2026-10-13 against sleepnumber-scraper, from an address the
    site genuinely does refuse, so the conclusion even looked right:

        --retries 0   exit 3, 39 bytes, 36 ms    <- never left the process
        --retries 1   exit 3, 923 bytes, 116 ms  <- the real CloudFront 403

    A network round trip does not finish in 36 ms. Three third-party audits of
    the 2scraper scrapers have now run `--retries 0`, read that output, and
    reported a working scraper as broken or a site as unreachable.

    The flag counts ATTEMPTS, not re-tries, so a caller who writes 0 almost
    certainly means "do not retry", which is 1. Clamping would hide the
    mistake; saying so ends the ambiguity.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _argparse.ArgumentTypeError(
            f"--retries must be an integer, not {value!r}")
    if number < 1:
        raise _argparse.ArgumentTypeError(
            f"--retries must be at least 1, not {number}. It counts ATTEMPTS, "
            f"not re-tries, so --retries 1 means 'try once and do not retry'. "
            f"With 0 the load loop never runs, nothing is fetched, and the "
            f"engine reports the browser's blank page as a result.")
    return number


def bounded(kind, minimum, flag: str):
    """argparse `type=` for a numeric flag that has a floor.

    A negative --delay reached `time.sleep` as a ValueError mid-run, a
    negative --proxy-block-retries silently meant "never rotate", and
    --pages 0 or --concurrency 0 asked for nothing. Each is refused at parse
    time, as a usage error (exit 2) that names the flag, in every entry
    point.
    """
    def check(value):
        try:
            number = kind(value)
        except (TypeError, ValueError):
            raise _argparse.ArgumentTypeError(
                f"{flag} must be a{'n integer' if kind is int else ' number'}, "
                f"not {value!r}")
        if number < minimum:
            raise _argparse.ArgumentTypeError(
                f"{flag} must be at least {minimum}, not {value}")
        return number
    check.__name__ = kind.__name__  # argparse names the type in its errors
    return check


def env_url_conflict(env_url: str, mode: str, club_id=None, player_id=None,
                     season=None) -> Optional[str]:
    """Why a TRANSFERMARKT_URL from the environment or `.env` must not stand
    in for the URL this run would build, or None when it may.

    The loader fills only unset values, so after it runs a URL from `.env`
    looks exactly like a typed --url. With the market-values URL the example
    file used to ship, `--mode club-squad --club-id 27` silently scraped the
    ranking and labelled it club 27's squad. A builder flag the user typed,
    or a URL that addresses a different page kind than --mode, therefore
    wins over the environment. A typed --url is never second-guessed here.
    """
    from product_parser import url_kind
    typed = [flag for flag, value in (("--club-id", club_id),
                                      ("--player-id", player_id),
                                      ("--season", season)) if value]
    if typed:
        return f"{'/'.join(typed)} was given on the command line"
    kind = url_kind(env_url)
    if kind is not None and kind != mode:
        return f"it addresses a {kind} page, and this run is --mode {mode}"
    return None
