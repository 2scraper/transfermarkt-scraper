# transfermarkt-scraper

[![release](https://img.shields.io/github/v/release/2scraper/transfermarkt-scraper?sort=semver)](https://github.com/2scraper/transfermarkt-scraper/releases)
[![tests](https://github.com/2scraper/transfermarkt-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/transfermarkt-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/transfermarkt-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/transfermarkt-scraper/actions/workflows/canary.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![licence](https://img.shields.io/badge/licence-MIT-lightgrey)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20CDP%20%7C%20Scraper%20API-informational)](#engines)

Scrapes **transfermarkt.com**: player market-value rankings, a club's full
squad, the latest-transfers ledger, and individual player profiles. JSON or
CSV, two row schemas (players and transfers — see below for why not one),
and a run-metadata sidecar that says whether the result is complete.

**The documented default path is 2Captcha's Scraping Browser API**, over
`--cdp-endpoint`: a browser you neither run nor patch, carrying its own exit
and a persistent profile, so it answers "where do I run this from" and "what
launches Chromium" at once. The **captcha solver** (`--twocaptcha-key`) sits
behind it as the fallback, for a challenge that reaches the page anyway.

That is the default because *getting* the page is the part that depends on
where you run from. This site is fronted by **AWS WAF on CloudFront**, and
whether it challenges you is a property of your address on the day, not of
the site — so this repo ships the probe that answers it for your address
instead of a number to trust:

```bash
bash tools/waf_probe.sh 10 market-values
```

If that comes back clean, a local engine needs neither product. See
"What the 2Captcha products are for here" for what each one buys.

Engines: **Playwright** (the primary local engine, and the only one with
`--concurrency`), **Selenium**, **pyppeteer**, and **`scraper_api_client.py`**,
a plain-HTTP fourth engine that talks to 2Captcha's **Scraper API** directly.
The pages themselves need no JavaScript, so the browserless engine parses
them completely.

---

## What this repo does, in four modes

```bash
# The market-value ranking, page 1
python3 playwright_scraper.py --mode market-values

# A club's full squad — id is transfermarkt's own numeric club id
python3 playwright_scraper.py --mode club-squad --club-id 281 --season 2025

# The latest-transfers ledger, 3 pages (safe to run with --concurrency)
python3 playwright_scraper.py --mode transfers --pages 3 --concurrency 3

# One player's full profile
python3 playwright_scraper.py --mode player --player-id 418560
```

`--mode` decides everything else about the URL: pass `--club-id`/`--season`
or `--player-id` and the scraper builds the right page, or pass `--url`
directly if you already have one.

---

## Two kinds of row, not one

Every row here is either a **Player** (market-values, club-squad, player) or
a **Transfer** (transfers) — not one dataclass trying to be both. A market
value and a football position have no equivalent on a transfer record, and a
transfer fee and two club names have none on a player row; folding both into
one schema would mean a permanently-null column on every run of one mode,
which is exactly the anti-pattern this repo's sibling e-commerce scrapers
learned to avoid. See `output_writer.py`'s docstring for the full reasoning.

Both schemas open on the same short family prefix (`source`, `scraped_at`,
`url`, `sku`, `title`, `image_url`, `price`, `currency`, `category`), so
something written against another repo in this family reads the first nine
columns unchanged.

```json
{
  "source": "transfermarkt.com",
  "scraped_at": "2026-09-10T08:21:28.159063+00:00",
  "url": "https://www.transfermarkt.com/x/profil/spieler/937958",
  "sku": "937958",
  "title": "Lamine Yamal",
  "image_url": "https://img.a.transfermarkt.technology/portrait/small/937958-1773173768.jpg?lm=4711",
  "price": 220000000.0,
  "currency": "EUR",
  "price_source": "listing",
  "category": "market-values",
  "rank": 1,
  "page": 1,
  "row_index": 0,
  "position": "Right Winger",
  "shirt_number": null,
  "age": 19,
  "nationality": "Spain",
  "nationalities": ["Spain"],
  "club": "FC Barcelona",
  "club_id": "131",
  "full_name": null,
  "birth_date": null,
  "...": "(the rest of the detail-only fields, null on a listing row)"
}
```

Full sample: [`sample_output.json`](sample_output.json) /
[`sample_output.csv`](sample_output.csv) — cut from a real capture's parse,
not written by hand.

---

## No JSON-LD anywhere on this site

Checked on every page kind this repo covers — a ranking page, a squad page,
a transfer list, and a player profile — **zero**
`<script type="application/ld+json">` blocks on any of them. That is a real
difference from this family's e-commerce members, which read structured
data first and fall back to the DOM. Here there is no first path to fall
back FROM; every field comes from one of three anchors, all measured rather
than assumed:

1. **URL patterns** — `/profil/spieler/{id}`, `/verein/{id}`,
   `/jumplist/transfers/spieler/{id}/transfer_id/{id}` — a contract with
   search engines, and the most durable hook this repo has.
2. **`schema.org/Person` microdata** on a player's profile header
   (`itemprop="birthDate"`, `"nationality"`, `"height"`, `"affiliation"`) —
   real structured data, just not JSON-LD.
3. **The site's own class names** (`table.items`, `posrela`, `hauptlink`,
   `data-header__label`) or column position, everywhere else.

Money is simpler here than for a shopping site: every value is in EUR
(checked on this English-language `.com` site, which still prints `€`
rather than converting), with a lowercase `k`/`m`/`bn` suffix convention and
a period decimal separator. `"-"` means no value published and parses to
`None`, never `0`.

---

## Install

```bash
git clone https://github.com/2scraper/transfermarkt-scraper
cd transfermarkt-scraper
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium
```

**Install exactly one engine.** The three declare mutually unsatisfiable
pins (playwright and pyppeteer disagree on `pyee`, pyppeteer and selenium on
`urllib3`). They do run side by side in practice, because neither library
touches the incompatible part — but `pip check` reports the conflict and pip
may resolve it by downgrading something you wanted. Use a virtualenv per
engine if you need more than one (this is exactly what CI's engine-smoke job
does).

Run the offline suite first. It needs no network, no browser and no
credentials:

```bash
python3 smoke_test.py
```

---

## Usage

The default path — the Scraping Browser API, no local Chromium, its own
exit:

```bash
python3 playwright_scraper.py --mode market-values --pages 2 \
  --cdp-endpoint "ws://{login}-zone-scraping_browser-country-gb-pid-{profileId}:{password}@cb.2captcha.com:9222"
```

That is the endpoint's SHAPE, not a working one. A Scraping Browser
profile's credentials are short-lived, so fetch fresh ones from your
2Captcha dashboard and put them in `.env` as `TRANSFERMARKT_CDP_ENDPOINT` —
every engine reads it from there, and then the flag above is not needed on
the command line at all. `country-` picks the exit country; `pid-` is a
profile with persistent cookies, one live connection each, so reuse a pid
rather than minting one per run.

The same four modes, run locally — which is all you need from an address the
site does not challenge (`tools/waf_probe.sh` tells you whether yours is
one):

```bash
# Market values, first 2 pages — see "the gallery pagination trap" below
# for why this mode chains pages one at a time inside a single session
python3 playwright_scraper.py --mode market-values --pages 2

# A club's squad for a specific season
python3 playwright_scraper.py --mode club-squad --club-id 281 --season 2025

# Latest transfers, safe to fetch concurrently (independently addressable)
python3 playwright_scraper.py --mode transfers --pages 5 --concurrency 4

# One player's full profile — birth date, agent, contract, caps, all of it
python3 playwright_scraper.py --mode player --player-id 418560

# Through a pool of exits, if you have proxies from anywhere
python3 playwright_scraper.py --mode transfers --pages 10 --proxy-file exits.txt

# The fallback: solve a challenge that reached the page anyway. The key
# comes from TWOCAPTCHA_KEY in .env — never from argv, which `ps` can read
python3 playwright_scraper.py --mode market-values
```

Credentials belong in `.env`, never on a command line — a secret in `argv`
is readable by anything that can run `ps` and lands in your shell history.
Copy `.env.example` to `.env` and run `python3 env_config.py` to see what
was picked up (it prints no secrets).

---

## The gallery pagination trap (market-values) — rare, not the default

First measured **2026-09-10** against
`/spieler-statistik/wertvollstespieler/marktwertetop`, and originally
written up as the deterministic shape of this mode's pagination:

- The page's own `<link rel="next">` and its "2" pagination link both carry
  a **doubled path segment** — `.../galerie/{uuid}/page/2//page/2` — which
  404s verbatim.
- Fixing the doubled segment by hand does **not** fix it either: fetched as
  a fresh, cookie-less request, the corrected URL returns page 1's content
  again, not page 2.

That capture was real. **What was wrong was treating it as the norm: a
wider re-measurement (2026-09-10) found 28 of 29 consecutive fresh fetches
of the same URL returned the PLAIN `?page=2` shape — identical in form to
`/statistik/neuestetransfers` below — and only one showed the
`galerie/{uuid}` shape.** So this is a real but INTERMITTENT anomaly
(roughly 1 in ~29 in that sample; treat the ratio as indicative, not
precise — nothing here explains why the site serves one shape or the
other), not this mode's normal behavior. And in the plain-shape case, a
stateless fetch of the constructed `?page=2` genuinely works: 0 shared
player ids against page 1, 25 rows each.

So `page_flow.pagination_is_addressable()` now runs the SAME check for
market-values that it always ran for transfers — comparing page 1's own
next-link against what `page_url()` would construct — instead of a
hardcoded `False` for this mode alone. **In the common case this mode is
independently addressable, same as transfers.** Only when page 1's own
next-link disagrees (the rare gallery shape) does this scraper fall back to
chaining the site's own pagination link one page at a time **inside a
single browser session**, exactly as before — the fallback did not change,
only when it engages did. `--mode market-values --pages N` with `N > 1`
uses whichever path page 1 says to use; `--concurrency` remains
`transfers`-only regardless (see Engines below) — not because this mode is
usually unaddressable, but because running several workers concurrently
against a mode that can rarely switch shape mid-run is untested here.

**`/statistik/neuestetransfers` was and remains simpler.** Its
`<link rel="next">` is a bare `?page=2`, and page 2 fetched statelessly
shared **0** transfer ids with page 1 (25 rows each, measured 2026-09-10) —
genuinely, independently addressable on every capture taken of it so far.
That is the one mode `--concurrency` is enabled for.

---

## Traps that look like bugs

**A club-squad run has fewer priced rows than roster size.** Measured on
Manchester City's 2025/26 squad: 43 players, **36 priced**. The other 7 are
real — youth-squad or fringe players with no published market value — and
they report `price: null`, not `0`. A squad-value total built from this data
should skip nulls, not treat them as zero.

**Dual nationality shows two flags, and `nationality` is only the first
one.** Kylian Mbappé's row carries `nationalities: ["France", "Cameroon"]`;
`nationality` (singular) is the first, for a consumer that wants one value
rather than a list. Neither is more "correct" — the site shows both flags on
the row and this repo keeps both.

**A transfer's `from_club` said "Without ClubWithout Club" during
development.** That was a real bug, now fixed, and worth knowing if you
extend the parser: the site's own markup carries a doubled `title` attribute
on the "Without Club" free-agent placeholder (`title="Without
ClubWithout Club"`), AND a cell can hold two `<a>` tags for the same
club — an image-only wrapper (empty text) and the real text-bearing anchor.
Reading the first matching link picked the image wrapper's empty text, so
`_detitle()`'s doubled-title guard never had real text to fall back to. Fixed
by reading `td.hauptlink a` specifically. See `product_parser.py`'s
`_club_cell_with_league` and `_detitle` docstrings.

**`joined_date` and `contract_until` came back null on every player, even
though the page shows them.** Also fixed, also worth knowing: the profile
header uses `data-header__label` on BOTH `<li>` tags (birth date, position,
agent) AND `<span>` tags (Joined, Contract expires) — the same class on two
different elements is a genuine markup inconsistency on the site's part, and
selecting only `li` silently dropped the latter. See `_label_map`'s
docstring.

**Only five hosts are supported** (`www.transfermarkt.com`, `.de`, `.world`,
`.co.kr`, `.jp`) **— not every locale Transfermarkt's own hreflang set
lists.** A same-brand host is not proof of the same platform (this family's
own `mediamarkt.lu` lesson), so each of the five above was fetched live and
its markup checked against the English site before being added; the other
~20 (`.es`, `.co.uk`, `.fr`, ...) were not, and a URL on one of them is
refused with a reason naming exactly this, not silently attempted. See
"Locales" below for what's supported and how to use it.

---

## Locales

v0.1 of this repo verified only `www.transfermarkt.com`. Five hosts are
verified as of v0.3 — the same DOM structure and URL/id scheme, just
different text, normalised into a single English vocabulary:

| Host | Locale | Money format | Position/nationality text |
|---|---|---|---|
| `www.transfermarkt.com` | English | `€220.00m` | English (nothing to translate) |
| `www.transfermarkt.de` | German | `220,00 Mio. €` | Translated (10 positions, 14 nationalities measured) |
| `www.transfermarkt.world` | Russian | `220,00 млн €` | Translated (7 positions, 8 nationalities measured) |
| `www.transfermarkt.co.kr` | Korean | `220.00 mil. €` | Translated (7 positions, 8 nationalities measured) |
| `www.transfermarkt.jp` | Japanese | `220.00 m €` | Positions translated; nationality text is already English on this locale (a real site asymmetry, not a gap) |

There is no separate `--locale`/`--country` flag — same convention as this
family's `mediamarkt-scraper`. The locale is read from whichever host you
pass in `--url`:

```bash
# German market-values ranking -- same rows as the English one, translated
python3 playwright_scraper.py --mode market-values \
    --url "https://www.transfermarkt.de/spieler-statistik/wertvollstespieler/marktwertetop"

# A player profile fetched from the Russian-language site
python3 playwright_scraper.py --mode player \
    --url "https://www.transfermarkt.world/x/profil/spieler/418560"
```

`--mode club-squad --club-id ...` and `--mode player --player-id ...` (no
explicit `--url`) always build against the English site — pass a full
`--url` to reach a locale directly, the same way `--mode market-values`
without `--url` only ever reaches the English ranking page.

What gets normalised, and how:

- **Money** — `parse_market_value` picks the decimal separator, symbol
  position and suffix vocabulary per locale. Every value is EUR in every
  locale (Transfermarkt does not convert); only the formatting differs.
- **Position / nationality** — translated to English via a per-locale
  lookup table built from real captures (a shared player's row matched
  across a locale and the English site, text diffed at the identical DOM
  position). An untranslated string (not yet measured for that locale)
  passes through unchanged with a logged warning — never guessed.
- **Player-detail labels** (joined/contract-expires/full-name/place-of-birth/
  the market-value "last update" prefix) — read via the locale's own label
  text, measured from a live capture of the same player (Erling Haaland,
  id 418560) in each locale.
- **Dates** — normalised to ISO 8601 (`YYYY-MM-DD`). The five locales don't
  just use different separators, the field ORDER differs (en/de/ru are
  day-first, ko/ja are year-first), and Russian spells the month out in
  genitive case (`"21 июля 2000 г."`) rather than using digits.
- **`source`** — every row is tagged with the actual host fetched
  (`site_host(base_url)`), not hardcoded to the English one.

**Known gaps, stated rather than silently assumed:**

- **Coverage is uneven per locale.** German has the fullest tables (10
  positions from a full squad-page capture); Russian/Korean/Japanese have
  7-8 (from an attacker-heavy top-25 ranking page — no goalkeeper or
  full-back sampled yet for those three). A position/nationality this repo
  has not seen yet passes through untranslated with a logged warning rather
  than a guessed translation.
- **The transfers listing's "Loan transfer"/"Free transfer" text is matched
  in English only** — no non-English transfers-listing capture exists yet
  to measure the de/ru/ko/ja equivalents from. A locale transfers page
  would report those rows' `fee_type` as `"unknown"` rather than
  mis-classify them.
- **Club names are not translated.** Unlike positions/nationalities (a
  small, closed vocabulary), club names are effectively open-ended —
  translating them would mean fabricating a dictionary for clubs never
  seen, which this repo will not do.
- **Billion-scale money suffixes** (`bn`/`Mrd.`/`млрд`/`bil.`) are
  documented-but-unmeasured in every locale, English included — they only
  appear in an aggregate table this repo does not parse. Kept because the
  format costs nothing to accept.

---

## What the 2Captcha products are for here

> **Corrected in v0.4.1.** Up to v0.4.0 this section said a single clean
> fetch of each page kind meant none of these products was needed here.
> That was one measurement from one network position, written up as a fact
> about the site. Transfermarkt sits behind **AWS WAF on CloudFront**, and
> from a bare datacentre address 8 of 10 consecutive requests were answered
> with a captcha rather than the page. If you read the old version and
> concluded a key would not help you, it was wrong.
>
> **And that figure has its own date.** It was measured early on
> 2026-09-16. Later the same day the rule appears to have been off: six
> distinct network positions — a home connection and four proxied exits in
> `us`/`de`/`in`/`br`, one of them with TLS impersonation disabled — drew
> **0 challenges in 15 requests**, and a re-run of `tools/waf_probe.sh 10
> market-values` from a home connection at 17:17Z that day was **0 of 10**,
> every response `http=200 server=nginx`. Neither number is a fact about the
> site. **Run the probe from your own address**; that is why it ships.

The order below is the order to reach for them in.

| Product | What it is for |
|---|---|
| **Scraping Browser API** (`--cdp-endpoint`) — *the default path* | A browser you neither run nor patch, carrying its own exit and a persistent profile, so it answers the address question and the browser question at once. Its auto-solve extension ships an AWS WAF interceptor, so it may clear a challenge before the local solver is offered one (`Captcha.setAutoSolve`; enabling it is confirmed on this site, clearing a challenge is not — no challenge has been met to clear). |
| **Captcha solving** (`--twocaptcha-key`) — *the fallback* | 2Captcha's API solves the AWS WAF captcha this site serves, via task type `AmazonTaskProxyless` — all four parameters it needs are published in the challenge page's own `window.gokuProps`, and the task schema is verified against the API with a control. Implemented in `captcha_solver.detect_aws_waf`; **this repo has not yet run it against a live challenge**, because no challenge has been met since the code path was written. `BOT_CHALLENGE_MARKERS` also covers Cloudflare, DataDome, PerimeterX/HUMAN, reCAPTCHA and hCaptcha. |
| **Proxies** (`--proxy-file`) | An exit that is not your own, without a remote browser — worth reaching for when you already run Chromium somewhere and only the address is the problem, or when you want volume spread over many exits. Measured 2026-09-16: the real page returned from a 2Captcha proxy exit with no solve needed. |
| **Fingerprints** (`--fingerprint`) | A consistent device identity across runs. |

If you already have proxies from somewhere else, `--proxy-file` takes any
list.

---

## Engines

The three browser engines produce the same rows, the same exit codes and the
same run status; the decisions that determine them live in `page_flow.py`
and `output_writer.finish_run()` so they cannot drift apart. Playwright is
the primary engine and the only one with `--concurrency`.

Known limits, stated here rather than left to be discovered:

- **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
  `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
  `ws://user:pass@host:port` and authenticate on the WebSocket upgrade;
  chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
  put a password.
- **Selenium cannot authenticate a proxy at all.** This repo strips
  credentials and warns rather than letting you believe a `user:pass` URL
  is doing something.
- **pyppeteer is effectively unmaintained** and its own README points at
  Playwright. It is here for parity with this family's usual three-engine
  lineup.
- **On Apple silicon, pyppeteer needs `--chromium-path`.** The Chromium it
  downloads for itself (revision 1181205) is an **x86_64** build. Under
  Rosetta it prints its DevTools URL and then dies —
  `mach_port_rendezvous ... Unknown service name` — which pyppeteer reports
  as the unhelpful `Browser closed unexpectedly`. pyppeteer reads no
  environment variable for an alternative binary, so pass one:

  ```bash
  python3 puppeteer_scraper.py --mode market-values \
    --chromium-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  ```

**All three engines, and the Docker image, were run against the live site on
2026-09-16 and their rows compared with `diff_runs.py`: 0 added, 0 removed,
0 changed in every pairing.** That is what the sentence above about them not
drifting apart is worth — before this release two of the three had never
completed a live fetch of this site at all.

### The fourth engine: `scraper_api_client.py` (no browser)

```bash
python3 scraper_api_client.py --mode market-values
python3 scraper_api_client.py --mode transfers --pages 3
```

Talks to 2Captcha's separate **Scraper API** (`scraper.2captcha.com`) over
plain HTTPS instead of driving a browser — no Chromium to install, no CDP
plumbing. It exists here because it is an unusually good fit for this site
specifically, not just for family parity: a plain, unauthenticated fetch of
all four page kinds returned complete, current HTML with no challenge markup
(measured 2026-09-10, and cross-checked again while porting this file — see
`product_parser`'s own docstring). `--cdp-url` is wired in for the day that
stops being true, but this repo has not needed it.

Two things it cannot do that the browser engines can:

- **No `--mode market-values --pages N > 1`.** That mode's pagination needs
  a stateful browser session (see "the gallery pagination trap" above); a
  stateless HTTP client cannot chain it. Asking for more than page 1 here is
  exit `6` (partial), not a silent short result.
- **No `--fingerprint`.** Fingerprint spoofing patches a live browser
  context; there is no browser context here to patch (route through
  `--cdp-url` if a browser identity matters — the remote session already
  carries its own, the same reasoning `fingerprint_client.py`'s docstring
  gives for not layering one on top of the Scraping Browser API).

Everything else — `--mode transfers --pages N` (stateless, independently
addressable), the same four `--mode` values, the same output contract and
`.meta.json` sidecar, the same `.env` variables — works the same as the
browser engines.

### Concurrency

`--concurrency N` fetches pages through N parallel workers, and it is only
reachable for `--mode transfers` — the one mode this repo measured to have
independently addressable pagination (see the gallery pagination trap
above). A worker owns one browser and one exit for its lifetime, and each
starts on a *different* exit, so no thread needs a lock. Page 1 is always
fetched alone, before the rest are dispatched.

`--concurrency` is refused outright with `--cdp-endpoint` (a Scraping
Browser profile allows one live connection at a time — several `pid`s, one
run each, is the way) and warns rather than refuses with no proxy pool (N
workers then send N× the traffic from one address).

---

## Docker

```bash
docker build -t transfermarkt-scraper .
docker run --rm -v "$PWD/out:/out" transfermarkt-scraper \
  --mode market-values --pages 2 --out /out/market_values
```

The image carries the Playwright engine and its own Chromium. Nothing is
baked into it: pass `--proxy`/`--cdp-endpoint` the same way as locally, or
mount a `.env` at `/app/.env`.

Built and run for the first time on 2026-09-16, and all four things worth
checking were checked rather than assumed — the image builds; the entrypoint
answers `--help`; Chromium really launches inside it (153.0.8010.12, not an
`apt-get` layer that merely exited 0); and the image contains no `.env`, no
test suite and no fixtures. A container then scraped the live site and
returned rows identical to all three local engines. CI rebuilds it on every
push for the same four reasons.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Complete |
| `1` | Crash |
| `2` | Bad usage (including a URL that is not a supported Transfermarkt host) |
| `3` | Blocked — a bot-check/challenge page |
| `4` | Zero rows |
| `5` | Remote API error — the Fingerprint API or a `--cdp-endpoint` connection failed on its own terms (bad key, rate limit, a Scraping Browser profile already in use). Distinct from `3`: this is a 2Captcha product call failing, not Transfermarkt refusing a page. A captcha-SOLVE failure is deliberately NOT this — it's a warning and the run continues, reporting `3` only if the page really was blocked. |
| `6` | Partial — some pages fetched, then stopped early. Not necessarily a fault: for `--mode market-values`, this is the expected result on the rare run whose pagination link needs the fallback chain (see "the gallery pagination trap") and runs out of it; on the common run it does not happen at all. |

**A run that finds nothing writes nothing.** Last known good output is not
replaced with `[]`; `--allow-empty` is the opt-out. `<out>.meta.json`
records `status`, `stop_reason`, `mode`, `source` and **which** pages
failed by number.

---

## Comparing two runs

```bash
python3 diff_runs.py --old monday.json --new tuesday.json --fail-on-change
```

Reports added, removed and changed rows by `sku` — which means a
*player* id for market-values/club-squad/player runs, and a *transfer's own*
id for transfers runs (a player can transfer more than once, so the player
id is not unique there — see `output_writer.py`). Refuses to compare two
runs that are not both `complete`, or that are different modes.

---

## Known limitations

Stated plainly rather than discovered the hard way:

- **The ORIGINAL build/test environment for this repo could not complete a
  live browser connection to www.transfermarkt.com at all** — both a direct
  `requests` call and a local Playwright browser failed at the TCP/TLS
  layer there, traced to that *sandbox's own outbound allowlist* rejecting
  the destination, not to anything Transfermarkt did. **A second, separate
  environment (2026-09-10, during this v0.1 review) reached the site with a
  plain, unauthenticated `requests`-style fetch — no browser, no proxy, no
  `--cdp-url` — for all four page kinds**, each a complete, current page
  (120-360KB) with no challenge markup, and `product_parser`'s parsing
  functions run directly against that HTML produced the same shapes of row
  this README and `sample_output.json` report. That is real evidence a
  plain HTTP path works from AT LEAST ONE non-datacentre-flagged network,
  which is why `scraper_api_client.py` (the browserless fourth engine,
  below) exists and needs no `--cdp-url` by default. **That gap is now closed for
  all three browser engines and for the Docker image**: live runs from a
  home connection on 2026-09-16 each returned 25 rows at 100% price coverage
  from `--mode market-values --pages 1`, plus 50 rows from
  `--mode transfers --pages 2` and one full profile from
  `--mode player --player-id 418560`, every run `status=complete` and every
  pairing identical under `diff_runs.py`. Whether a known-datacentre IP is
  treated differently is a question about an address, and
  `tools/waf_probe.sh` is how you answer it for yours.
- **The WAF solve path has never met a live challenge.** `AmazonTaskProxyless`
  is implemented and its schema is verified against the API with a control,
  but no challenge has appeared to solve since it was written, so the
  end-to-end path is untested. Stated here rather than left to be
  discovered: this is "not yet exercised", not a verdict on the solver.
- **Selenium cannot use the Scraping Browser API** (see the engine limits
  above), so on an address that meets the WAF, Playwright or pyppeteer is
  the engine to reach for. The solve path itself is now identical in all
  three.
- **Five hosts are supported** (`.com`, `.de`, `.world`, `.co.kr`, `.jp` —
  see "Locales" above). Other locale sites (`.es`, `.co.uk`, ...) were not
  checked and are refused with a reason.
- **Locale coverage is uneven and the transfers-listing fee-type text is
  English-only** — see "Locales" above for exactly what is and is not
  translated per locale.
- **`scraper_api_client.py` (the browserless fourth engine) only paginates
  `--mode transfers`.** `--mode market-values --pages N > 1` needs a
  stateful browser session (see "the gallery pagination trap") that this
  HTTP-only client, by design, does not keep — it fetches page 1 and
  reports `partial` (exit 6) rather than silently stopping short and
  calling that `complete`. Use one of the three browser engines for that
  case.
- **Three modes, not the old scraper's ~24 categories.** This is a
  deliberate v0.1 scope decision (market-values, club-squad, transfers,
  plus player detail as the natural fourth mode the other three need to
  enrich a row) rather than porting every category the previous version of
  this project covered.

---

## Troubleshooting

**`--mode market-values --pages N` reports `partial` past page 1.** Expected
in some cases, not necessarily a bug — see the gallery pagination trap. Check
`stop_reason` in the `.meta.json` sidecar.

**Every page comes back exit 3.** Run `bash tools/waf_probe.sh 10` from the
same machine. If it reports challenges, the address is the problem, not the
site, and `--cdp-endpoint` or `--proxy-file` is the answer; if it reports
none, the site's markup or this repo's detection has changed and the HTML
dump from `--dump-html` is what tells you which.

**`--cdp-endpoint` connects, `Captcha.setAutoSolve` reports enabled, and
then every navigation fails `ERR_TUNNEL_CONNECTION_FAILED`.** This is the
REMOTE browser's exit failing, not your own network — the engines say so in
the error rather than leaving you to guess. Observed on one account's zone
on 2026-09-16, independent of the `country-` segment, and selective by host:
`example.com` returned 200 from the same session in which
`www.transfermarkt.com` and `api.ipify.org` both failed this way. A zone
whose upstream refuses CONNECT for some hosts is an account/zone
configuration matter to raise with 2Captcha support — there is nothing to
fix in this repo, and a run with the flag removed will use a local browser.

**`--mode club-squad` returns very few rows.** Check the club id and
`--season` are what you intended — an off-season or lower-tier squad
legitimately has fewer priced players.

More in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Measurements in this README

Everything above with a number attached was measured against real captured
pages on **2026-09-10** (a market-values ranking page, Manchester City's
squad, two pages of the latest-transfers listing, and a player profile),
via a single proxied fetch per page kind. The offline suite pins the field
values from those same captures — including the dual-nationality case, the
null-market-value case, every `fee_type` this repo classifies, and the two
parsing bugs this project found and fixed during development — so a change
in the site's markup fails a test rather than quietly emptying a column.
The Locales section above was measured the same way on **2026-09-16**,
against the four additional hosts (a market-values ranking page in each,
a full club-squad page in German, and Erling Haaland's own profile in all
four).

`smoke_test.py` needs no network, no browser and no credentials. Its total
depends on how many engine libraries are installed, because an engine whose
library is absent is reported as a skip rather than silently passing — so
read the number off your own run rather than off this paragraph:

```bash
python3 smoke_test.py | grep -c '^  PASS'
```

Measured 2026-09-16 on Python 3.14, one virtualenv per engine (the same
shape CI's `engine-smoke` matrix uses, and the only shape that works — the
three engines' pins are mutually unsatisfiable in one environment):

| Installed | Checks |
|---|---|
| nothing beyond `requirements.txt` | **459** |
| Playwright | **494** |
| Selenium | **481** |
| pyppeteer | **483** |

A configuration with fewer checks is not a weaker run; an engine whose
library is absent is reported as a skip, and CI fails if that list is not
empty.

---

## Legal

For research and analysis. You are responsible for complying with
Transfermarkt's terms, with `robots.txt`, and with the data protection law
that applies to you. This repo reads publicly rendered pages; it does not
attempt to reach anything behind an account.

MIT licensed — see [LICENSE](LICENSE).

Built by [2Captcha](https://2captcha.com).
