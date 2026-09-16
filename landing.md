# Transfermarkt scraper

**Market values, squads, transfers, player profiles — structured, on demand.**
→ one CLI, three engines, JSON or CSV

Open source · MIT · [github.com/2scraper/transfermarkt-scraper](https://github.com/2scraper/transfermarkt-scraper)

A scraper for transfermarkt.com built on real captured pages, not guesswork:
every field below was checked against the site before it shipped, and where
something turned out to be null, doubled, or session-gated, that behaviour
is documented rather than papered over.

[Clone the repo](https://github.com/2scraper/transfermarkt-scraper) · Python 3.9+

---

## You run this

```bash
python3 playwright_scraper.py --mode club-squad --club-id 281 --season 2025
```

## You get this

```json
{
  "sku": "418560",
  "title": "Erling Haaland",
  "position": "Centre-Forward",
  "shirt_number": 9,
  "age": 25,
  "nationality": "Norway",
  "price": 200000000.0,
  "currency": "EUR",
  "club": "Manchester City",
  "club_id": "281"
}
```

*One row of a real 43-player squad. Full sample: `sample_output.json`.*

---

## Four modes today

| Mode | What you get | Required |
| --- | --- | --- |
| `market-values` | The global most-valuable-players ranking, page by page | — |
| `club-squad` | A club's full roster: shirt number, position, age, nationality, market value | `--club-id` |
| `transfers` | The live transfer ledger: fee, fee type, from/to club and league | — |
| `player` | Full profile: birth date, height, agent, contract dates, caps, international goals | `--player-id` |

Four, not twenty-nine — this is a v0.1 release, scoped deliberately rather
than promising coverage that was not built and checked yet. Each mode above
was verified against a real page before it shipped: the market-values
ranking (25/25 rows priced), a full club squad (36 of 43 players priced —
the other 7 are real youth/fringe players with no published value, not a
parsing gap), two pages of the transfers ledger (0 duplicate transfer ids
between them), and a full player profile (every field, including the two
that needed a real fix during development — see below).

---

## Two kinds of row, not one padded schema

A market value and a transfer fee are not the same kind of fact, so this
scraper does not force them into one row shape with half its columns
permanently empty. Player rows (`market-values`, `club-squad`, `player`)
carry position, club, nationality, market value. Transfer rows carry both
clubs, both leagues, the fee and its type — disclosed, loan, free, or
undisclosed. Both share a common prefix (id, title, price, currency,
timestamp), so tooling written against one reads the other's shared columns
unchanged.

---

## Built from real pages, bugs and all

Two real parsing bugs were found and fixed while building this — not glossed
over, because they are exactly the kind of thing that costs someone else an
afternoon if left undocumented:

**A doubled club name.** A transfer's `from_club` came back
`"Without ClubWithout Club"` for a free-agent placeholder. The site's own
markup really does carry a doubled `title` attribute there, compounded by a
second `<a>` tag in the same cell that wraps only an image. Fixed by
reading the text-bearing anchor specifically.

**Missing contract dates.** `joined_date` and `contract_until` came back
`null` on every player profile, despite being visible on the page. The
header uses the same CSS class on two different tag types for these two
fields versus the rest — an inconsistency in the site's own markup, not a
missed selector. Fixed by matching both.

Both are now real cases in the offline test suite, pinned against the
capture that found them.

---

## Three engines, one contract

Same modes, same flags, same output shape. Use whichever your stack already
runs.

**Playwright — recommended.** The only engine with `--concurrency`, safe on
the one mode (`transfers`) this repo measured to have genuinely
independent, stateless pagination.

```bash
pip install playwright && playwright install chromium
python3 playwright_scraper.py --mode transfers --pages 5 --concurrency 4
```

**Selenium.** For teams already on WebDriver. Cannot authenticate a remote
CDP endpoint or a proxy — documented, not silently broken.

```bash
pip install selenium
python3 selenium_scraper.py --mode market-values --pages 2
```

**Puppeteer (via pyppeteer).** Puppeteer's API, in Python — not a Node.js
process.

```bash
pip install pyppeteer
python3 puppeteer_scraper.py --mode player --player-id 418560
```

---

## When a scrape needs more than a browser

**Proxies, if you need them.** Whether transfermarkt.com treats a datacentre
address any differently from a residential one is genuinely unmeasured for
this release — this project says so plainly rather than assuming the usual
story. `--proxy-file` takes a pool the moment you need one.
→ [Proxies at 2prx.com](https://2prx.com)

**A challenge page instead of data.** Cloudflare, DataDome, PerimeterX,
reCAPTCHA and hCaptcha are all detected. Pass `--twocaptcha-key` and the
scraper solves and continues on the same page.
→ [Solver at 2captcha.com](https://2captcha.com)

**A browser fingerprint that doesn't add up.** `--fingerprint` pulls a real
device fingerprint — user agent, screen, locale, timezone, all consistent
with each other — instead of a headless default that isn't.
→ [Fingerprint data](https://2captcha.com/s/fingerprints)

**A managed, already-fingerprinted browser.** Point `--cdp-endpoint` at the
Scraping Browser API and the same script runs against a hosted Chrome
profile with its own persistent cookies and exit country — no proxy flag,
no fingerprint flag, no local Chromium to patch.
→ [Scraping Browser API](https://2captcha.com/scraper/browser-api)

---

## Quick start

**Step 1 — install.**

```bash
git clone https://github.com/2scraper/transfermarkt-scraper
cd transfermarkt-scraper
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium
```

**Step 2 — run the offline suite** (no network, no browser, ~1 second):

```bash
python3 smoke_test.py
```

**Step 3 — scrape something real.**

```bash
python3 playwright_scraper.py --mode club-squad --club-id 281 --season 2025
```

**Step 4 — add proxies and solving, if you need them.**

```bash
cp .env.example .env   # fill in TWOCAPTCHA_KEY / TRANSFERMARKT_PROXY
python3 playwright_scraper.py --mode transfers --pages 10 --concurrency 4
```

---

## Questions

**Where do the ids come from?**
Transfermarkt's own URLs. `.../profil/spieler/418560` is player 418560;
`.../verein/281` is club 281 (Manchester City). Both are visible in any
club or player URL on the site.

**Will it break when the site changes?**
Eventually, yes — any scraper does. Selectors live in one file
(`product_parser.py`) shared by all three engines, so a layout change is
usually a few lines in one place. `--dump-html` writes the exact page a run
saw, so a report can include it.

**Do I need the paid add-ons?**
No. The scraper is complete and MIT-licensed on its own. Whether you need a
proxy at all is, honestly, something this release cannot tell you yet — try
without one first and see.

**JSON or CSV?**
Both, from the same run. List fields (a player's dual nationality) join
with `" | "` in CSV so a cell stays readable in a spreadsheet, and stay a
real list in JSON.

**What's not here yet?**
League tables, fixtures, match reports, coach and referee profiles, and
Transfermarkt's other locale sites (`.de`, `.es`, ...) — all out of scope
for this release, not silently broken. See the README's roadmap.

---

*This project collects publicly visible pages only. Transfermarkt is a
trademark of its owners and is not affiliated with this project. Read the
site's terms of service and `robots.txt`, keep request rates reasonable,
don't collect personal data you have no basis to process, and check the
rules that apply where you operate — how you use the output is your
responsibility.*

2scraper / transfermarkt-scraper · MIT ·
[GitHub](https://github.com/2scraper/transfermarkt-scraper) ·
[2Captcha](https://2captcha.com) · [2prx](https://2prx.com) · Built by the 2Captcha team
