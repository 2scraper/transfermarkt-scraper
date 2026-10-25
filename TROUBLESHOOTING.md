# Troubleshooting

Ordered by how often each thing actually happens, not by severity.

## Every page comes back exit 3 (blocked)

Check `--dump-html PATH` first — it writes the exact bytes the parser was
given, whether the run succeeded or not, so you can see what actually
arrived instead of guessing.

Transfermarkt sits behind AWS WAF, and whether it challenges you is a
property of your address on the day. Its *challenge* action (HTTP 202, no
widget) clears by itself in a real browser in about a second, and every
engine waits for that. Its *captcha* action (HTTP 405, a widget) does not.
Run `bash tools/waf_probe.sh 10 market-values` from the machine in question,
and see README "What the 2Captcha products are for here" for what each
product changes. A daily canary from a bare GitHub runner read the site
without any of them on every day from 2026-10-06 to 2026-10-15.

If a proxy does not help, check `detect_bot_challenge`'s markers
(`product_parser.BOT_CHALLENGE_MARKERS`) against the dumped HTML — they
cover Cloudflare, DataDome, PerimeterX/HUMAN, reCAPTCHA and hCaptcha, but
were not measured against a real challenge from this site, so a genuinely
new one could go unrecognised (falling through to the generic `"blocked"`
bucket via a non-2xx status).

## Exit 4 (zero rows) on a URL that plainly has data in a browser

Check `stop_reason` in the `.meta.json` sidecar (written with
`--allow-empty`) or the log first:

**`not_found`** — the site says the thing asked for does not exist. A club
id that does not exist gets the site's own "ERROR 404" page (HTTP 404); a
player id that does not exist is redirected to the market-values ranking
(HTTP 200), which the engines recognise because the id is gone from the
final URL. Check `--club-id` / `--player-id` / `--url`.

**`--mode club-squad`** — check the club id and `--season`. A season with no
squad data yet answers 200 with a page that has no player rows on it, which
is a correct "empty" answer, not a bug.

**`--mode market-values` / `--mode transfers`** past page 1 — you may be one
page past the end of the ranking/listing. The site answers a page number past
the end with the last page again (HTTP 200), so a page that brings no new
rows is not counted. If the page before it had no next link, that is the end
of the listing (`stop_reason: listing_ended`, exit 0, with `repeated_page`
and `listing_pages` in the `.meta.json` sidecar). An empty page ends the run
as `no_new_products`, also complete.

**`--mode transfers` stops after one page of about 20 rows.** On 2026-10-15
the default latest-transfers view (filtered by the site to players valued
EUR 500,000+) held 20 transfers: one page, no pager. That is the site's whole
listing for that filter. `--min-value 0` reads the unfiltered listing (4 pages
that day); see "How deep that listing goes" in the README.

## Exit 5 or 6 with `stop_reason: target_http_5xx`

Transfermarkt answered with a server error (an HTTP 5xx, or the edge's
155-byte nginx `502 Bad Gateway` page) and still did after 2 more tries on
the same exit, `--retry-delay` and then twice that apart. Seen intermittently
on 2026-10-15, one page at a time. It is not a block and not an empty
result; run again later.

## Exit 6 with `stop_reason: page_repeated`

A page linked to a next page, and the next page served rows already seen.
Pages were requested and not served, so the run is partial; the rows from
the pages that were served are kept.

## Exit 5 with `stop_reason: parser_found_nothing`

The page arrived and shows rows (a ranking or squad table linking to player
profiles, or a profile header), and the parser read none of them. That is a
markup change on the site, not an empty listing, so it is never reported as
exit 4. On a later page of a paginated run the same thing is exit 6 with the
rows gathered so far kept. `--dump-html PATH` saves exactly what the parser
was given.

## The row count is right but a column is empty

**`price`/`currency` is `null` on a real player.** For `market-values` and
`club-squad`, that is very likely a genuine "no market value published"
player (a young academy player, a very old veteran, a free agent) —
Manchester City's 2025/26 squad measured 36 of 43 priced, and the 7 nulls
were real. Check the player's own profile page before assuming a bug:
`"-"` in the market-value cell parses to `None`, never `0`, on purpose (see
`parse_market_value`'s docstring).

**`joined_date`/`contract_until`/`agent` are all `null` on a `--mode
player` row.** This project found and fixed exactly this bug once — the
profile header's `data-header__label` class appears on both `<li>` and
`<span>` tags, and an earlier version of `_label_map` matched only `li`. If
you see it again after a site change, check which tag the label moved to.

**`from_club`/`to_club` look wrong or doubled on a transfer.** See
`_club_cell_with_league`'s docstring — a transfer row's club cell can hold
two `<a>` tags (an image-only wrapper and the real text-bearing anchor), and
reading the wrong one produces exactly this kind of garbled name. Also
possible: the site's own `title` attribute is genuinely doubled for the
"Without Club" free-agent placeholder, which `_detitle()` exists to catch.

**`fee_type` is `"unknown"` on a lot of rows.** That is the fallback for a
Fee cell this parser does not recognise — check whether the site added a
new fee-presentation format beyond disclosed/loan/free/undisclosed ("?").

## `--mode market-values --pages N` reports `partial` (exit 6) past page 1

Check `stop_reason` in the `.meta.json` sidecar first.

Until 2026-10-21 the usual cause was the gallery pagination trap (README):
on 3 of 8 runs that day page 1's next-link was the `…/page/10//page/2`
shape, the scraper followed it, got 404 and stopped with `page_not_found`.
Market-values now ignores that link and fetches `?page=N`, which served the
real pages in every gallery session measured. So `page_not_found` on page 2
should no longer happen; if it does, the site has a pagination shape this
repo has not seen — please open an issue with the run's log (`--dump-html`
helps).

`page_load_timeout` or `target_http_5xx` on a later page is the network or
the site, not pagination: run again, or through `--cdp-endpoint` /
`--proxy-file`.

## "Blocked" on a page that clearly rendered

The Scraping Browser API's auto-solve extension injects its own scripts
into every page it loads, and those can contain a vendor string
(`cf-turnstile` is the one this family has hit before) that would otherwise
look like a real challenge marker on an otherwise clean page. This repo
strips `chrome-extension://`/`moz-extension://`-sourced script tags before
matching (see `product_parser._EXTENSION_TAG_RE`). If you see this anyway,
the stripping pattern may need widening for a new extension version.

## HTTP 500 from the Scraping Browser endpoint

Two causes look identical here, both met on 2026-10-21: another run still
holds the profile (`pid` — one live connection at a time), or the profile's
own exit failed (`proxy_error`, e.g. a dead proxy account behind
`our_proxy`). Wait for the other run or use a different `pid`; if a profile
nobody uses keeps answering 500, check its proxy in
`python3 tools/browser_profile_client.py accounts` / `profiles`.

The engines read the word in the 500's body: `profile_locked` is retried up
to 3 times (3, 6, 9 s) and `proxy_timeout` / `proxy_error` / `fingerprint_error`
once, so a 500 that reaches you has outlasted those retries.

A third cause names itself: `fingerprint_error` (met 2026-10-25) — the
service could not give the profile a fingerprint. It is neither a busy `pid`
nor the exit; the same profile connected a minute later.

Do not "check" a profile with `GET /json/version` before connecting: that
request takes the profile itself, and the connection that follows gets 500.

For parallel runs, use one profile per run (`--concurrency` is refused over
`--cdp-endpoint`).

## `ERR_TUNNEL_CONNECTION_FAILED` over `--cdp-endpoint`

The remote browser has no exit to tunnel through. Check the account's
`proxyMode` with `tools/browser_profile_client.py accounts` — `"none"` means
no proxy is attached, and the profile's `"inherit"` inherits that. This is
not a network problem on your machine, and not the site.

The CDP connection itself succeeding proves nothing here: authenticating to
the Scraping Browser and having an exit to browse through are two separate
things, which is why `Captcha.setAutoSolve enabled` can appear in the log
immediately before every navigation fails.

`use` refuses to write an endpoint for an account at `none` and says so.
Pick an account whose mode is `our_proxy`, or attach a proxy to this one.

## `401 deny_no_user` or `401 Wrong user name format`

The login in the connection string is malformed or stale. Fetch a fresh
`connectionUri` rather than editing the string:

```bash
python3 tools/browser_profile_client.py use --account-id N --write-env
```

Hand-assembling that login is what produces these. Measured 2026-09-17
against the live endpoint, three encodings of the same custom proxy in the
`-proxy-{base64url}` segment: padding stripped gave `401 deny_no_user`,
padding kept as `=` and padding as `%3D` both gave
`401 Wrong user name format`. Two distinct errors, and the last two agree,
so the complaint is about the username as a whole rather than the base64
inside it. The API builds a correct string itself; take that one.

## Selenium: `--cdp-endpoint` or `--proxy` does not work

Both are expected limits, not bugs — see README's Engines section.
chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
put credentials, and `--proxy-server=` accepts none either. Use
`playwright_scraper.py` or `puppeteer_scraper.py` for an authenticated
remote endpoint or an authenticated proxy.

## `pip check` complains after installing two engines

Expected — see requirements.txt. playwright and pyppeteer pin incompatible
`pyee` versions; pyppeteer and selenium want different `urllib3` ranges.
Install one engine per virtualenv if you need more than one.

## Something else

Run `python3 smoke_test.py` first — if it fails on a clean clone with no
`.env`, that is itself the bug and worth reporting as such. Otherwise open
an issue with the exact command (credentials replaced by `***`), the exit
code, and `--dump-html` output if the row count or a field looks wrong.
