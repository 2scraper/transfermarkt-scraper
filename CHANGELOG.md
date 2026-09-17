# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.0] — 2026-09-17

The release in which the **primary** path was finally exercised: a live AWS
WAF captcha, met and cleared by the Scraping Browser's own auto-solve, with a
control proving the block does not lift by itself. Also the release in which
several sentences this repo had been repeating turned out to be wrong.

### Corrected

> **v1.0.0 shipped a diagnosis that was wrong.** It said the Scraping
> Browser zone "refuses CONNECT selectively by host", called that an
> account/zone matter to raise with support, and stated there was nothing to
> fix in this repo. The symptom was real; the explanation was not
> established, and the conclusion it led to — that the documented primary
> path could not be made to work from here — was wrong.
>
> **The Browser API works.** Measured 2026-09-17 through `--cdp-endpoint`:
> 25 rows, `status=complete`, 25/25 priced, `Captcha.setAutoSolve enabled`.
>
> What is measured about the cause: one browser account on this key has
> `proxyMode: "none"` with `proxyAccountId: null`, and its default profile
> inherits that — no exit, so nothing to tunnel through. A second account
> has `proxyMode: "our_proxy"` against a live proxy account, and pointing
> `.env` at it worked on the first attempt. **That this explains the
> original failures is an inference, not an observation**: the endpoint that
> was in `.env` then was never hand-edited and predates both accounts, so
> which one it addressed cannot be recovered. It is a good inference. It is
> not a measurement, and it is not written here as one.

### Added

- **`tools/browser_profile_client.py`** — lists browser accounts, proxy
  accounts and profiles, and puts a ready `connectionUri` into `.env`
  (`use --account-id N --write-env`), so the endpoint never passes through a
  terminal or a shell history. It refuses to write one for an account whose
  `proxyMode` is `"none"`, and says why.
- README now documents the Browser API as **obtained, not assembled**, with
  the `proxyMode` table and the one command that shows which mode an account
  is in.
- TROUBLESHOOTING gains `ERR_TUNNEL_CONNECTION_FAILED over --cdp-endpoint`
  and `401 deny_no_user` / `401 Wrong user name format`, so both are
  findable by pasting the error.
- 16 offline checks covering the new client: a synthetic `HTTPError`,
  `ConnectionError` and `Timeout` each carrying `?key=<32 hex>` must lose the
  key in redaction — the GET endpoints take the key as a query parameter and
  `requests` puts the whole URL into the text of every one of those — and
  `safe()` must strip `password`, `login` and `connectionUri` from a fixture
  shaped like the real response, which is an object keyed `"0"`, `"1"`, …
  rather than an array.

### Fixed

- **`mask_url()` could raise on exactly the input that most needed masking.**
  It read `urlparse(...).port` bare; `urlparse` computes the port lazily and
  raises `ValueError` on a malformed authority, so the masker blew up and its
  caller was left holding the raw URI. This is the defect `proxy_pool.mask()`
  already documents as having put a live proxy login and password into a
  public CI log on a sibling repo, reintroduced in a new file. Now defensive
  in the same shape, and pinned by checks over five malformed inputs.

### Removed

- `tools/cdp_with_proxy.py`, which assembled the `-proxy-{base64url}` segment
  by hand. It did not work, and the API returns a ready `connectionUri`
  anyway. The finding is kept: measured 2026-09-17 against the live endpoint,
  three encodings of the same proxy URL — padding stripped gave
  `401 deny_no_user`, padding kept as `=` and padding as `%3D` both gave
  `401 Wrong user name format`. Two distinct errors, and the last two agree,
  so the complaint is about the username as a whole rather than the base64
  inside it. The vendor's one documented example encodes a 39-byte URL, a
  multiple of 3, so it needs no padding and cannot demonstrate the case.

### The AWS WAF solve, finally exercised — and the primary path was losing a race

**v1.0.0 and everything before it said the AWS WAF solve had never run
against a live challenge. It has now, and the repo had the two paths the
wrong way round.**

This project documents 2Captcha's Scraping Browser API (`--cdp-endpoint`,
`Captcha.setAutoSolve`) as the PRIMARY way a challenge is cleared, with the
solver API behind it as the fallback. The code did the opposite: on
detection it called the paid solver immediately. Measured 2026-09-17 on a
live AWS WAF captcha over the Browser API:

```
03:06:43  AWS WAF captcha found                      (the engine's detector)
03:07:35  2captcha returned existing_token           (~50s in the FALLBACK)
03:07:35  [Scraping Browser] CAPTCHA detected        (the primary, only now)
03:07:35  Set aws-waf-token — reloading
03:07:43  Blocked by AWS WAF
```

`Captcha.solveFinished` never arrived, because the fallback's reload had
already moved the page out from under the auto-solver. Three defects, each
measured and each fixed:

- **The fallback fired on detection.** The engine now waits for
  `Captcha.solveFinished` before offering anything to the solver API. The
  Captcha CDP events are counted rather than only logged — without somewhere
  to record them there was nothing to wait on.
- **The first wait budget was too short.** 45s, against a measured auto-solve
  time of 30–96s, so it timed out a minute early and the paid call ran
  anyway; its answer and `solveFinished` landed in the same second. Now 180s,
  twice the worst measured figure.
- **Reloading after a successful solve destroyed the result.** The
  auto-solver navigates the page itself once it has the token, and a reload
  issued alongside that returns `net::ERR_ABORTED; maybe frame was
  detached?` — measured one second after a `solveFinished` that had genuinely
  worked, leaving the run to parse a detached frame and report zero rows. The
  engine now waits for the page to paint, using the same polled count every
  other readiness wait uses.

**Result, with a control.**

| | Challenges met | Outcome |
|---|---|---|
| Control — `--no-autosolve`, nothing solving | 1 | **0 of 8** reloads cleared it |
| Primary path — auto-solve on | 5 | **5 of 5** → `solveFinished` → 25 rows, 25/25 priced, `status=complete` |

Auto-solve times: 30.0s, 51.5s, 58.0s, 63.0s, ~34s. The solver API was not
called in any of them. Cost is $0.00145 per solve, charged for the auto-solve
itself.

The control is what makes this a finding rather than a hope: section 19's
rule is that a block expiring on its own produces exactly the observation
"we solved it and the page came back". Here the same endpoint, on the same
site, with nothing solving, stayed blocked through eight consecutive
reloads.

### Added

- **`--no-autosolve`** on the two CDP-capable engines. It exists to make the
  control above possible: it lets a challenge be MET and left unsolved. It
  is a measurement mode, not a way to run a scrape, and says so.

### Fixed

- **AWS WAF has two rule actions, and this repo treated both as a captcha.**

  | Action | HTTP | `x-amzn-waf-action` | `captcha.js` | Body |
  |---|---|---|---|---|
  | CAPTCHA | 405 | `captcha` | present | 9.7–14 KB |
  | Challenge | 202 | `challenge` | absent | 2,409 bytes |

  Both carry `window.gokuProps`, so reading the props cannot tell them
  apart; the presence of `captcha.js` can, and the detector was already
  parsing it without using it. A challenge-action page renders no widget —
  section 19's "unsolvable is a property of a PAGE" — so sending one to a
  solver buys a token for a puzzle that was never there. `createTask`
  validates little enough to take the money: a fabricated task was accepted
  and charged. All three engines now refuse to send a widget-less AWS WAF
  page to the solver API, and the log names which action it is.

- **`AmazonTaskProxyless` was the wrong task type whenever the run has an
  exit.** It solves from 2Captcha's own address, which was not the address
  being challenged, so it returned `existing_token` and no
  `captcha_voucher` — $0.00145 for a token that cleared nothing, repeatedly.
  `AmazonTask` is the documented proxy-carrying variant
  (2captcha.com/api-docs/amazon-aws-waf-captcha, read 2026-09-17): the same
  fields plus `proxyType`, `proxyAddress`, `proxyPort` required and
  `proxyLogin`/`proxyPassword` optional. Given the page's own exit it
  returned a real voucher in ~20–25s. The engines now pass their current
  exit to the solver and the type is chosen from that.

### Fixed — the proxy filename trap, and a check that was red for the wrong reason

- **The project's own examples named files `.gitignore` did not protect.**
  `playwright_scraper.py`'s docstring passed `--proxy-file` a file called
  `proxies.txt`, and the README passed it one called `exits.txt`, while
  `.gitignore` protected the literal `proxylist.txt` and nothing else.
  (Written this way round on purpose: the suite check added below reads
  every `--proxy-file <name>` in the project's own files and demands git
  refuse that name, and it fires on this entry too if the old examples are
  quoted verbatim. Reword rather than allowlist — an allowlist is how a scan
  stops covering the thing it was written for.) Both were measured with
  `git check-ignore`, not by reading `.gitignore`: both came back
  committable. Anyone following our own example made an unignored file with
  a login and a password on every line. All documentation now names
  `proxylist.txt`, and the pattern is `proxylist*.txt` — wide enough for the
  variants people make, narrow enough that `requirements.txt` and
  `sample_output.csv` still commit. A broader shape such as `*proxy*.txt`
  would swallow files someone meant to keep, and `.gitignore` has no undo.

- **`ci_checks.py --secret` was red on a file it is not about.** Its claim is
  "no credentials **committed**", but it walked the whole directory and
  scanned gitignored files too, so the maintainer's own local
  `proxylist.txt` turned it red while CI — which never has that file —
  stayed green. A check that is permanently red for a reason nobody can fix
  is one everybody learns to ignore. It now asks `git check-ignore` which
  paths would be committed and skips the rest, saying how many it skipped.
  One subprocess for the whole list via `--stdin -z`, which also keeps
  filenames with spaces intact — this repo lives under a path with one.

  It degrades to scanning everything, silently and without a traceback, both
  outside a git repository and with `git` absent from `PATH`. This project
  ships as a zip as often as it is cloned, and a guard that takes the check
  down is worse than the gap it closes. All three states are pinned by
  checks.

  The startup guard originally sketched for this was dropped on the work
  order's own instruction: it was scaffolding for a file the documented
  default path no longer needs, now that a pool is generated from one
  credential rather than read from disk.

*Verified by regression:* reintroducing `proxies.txt` into the docstring
turns the suite red on that exact name, and removing it turns it green
again. A check that cannot fail proves nothing.

### Fixed — a duplicate key in `.env` answered differently per installed package

`load_env()` defers to `python-dotenv` when it can be imported and falls back
to its own parser otherwise, and `python-dotenv` is not in
`requirements.txt`. Measured 2026-09-17 on a fixture with three consecutive
`TRANSFERMARKT_PROXY` lines, through the DEFAULT `override=False`:

```
hand-rolled parser  ->  the FIRST value
python-dotenv       ->  the LAST value
```

Same file, same command, a different exit, and no error either way. The
hand-rolled loop assigned straight into `os.environ` and skipped any key
already present, so the first line won as soon as it had been assigned.

Fixed by **reporting**, the way `unknown_keys()` already reports a typo, and
by collapsing the file before consulting the environment so both branches
agree on the LAST occurrence — python-dotenv's answer and the shell
convention. The documented precedence is unchanged: a real environment
variable still beats the file. The warning names the key, every line it was
set on, and which line won; a duplicated secret is reported by key and line
and never by value.

The neighbours were checked at the same time, since two parsers that
disagree on duplicates may disagree elsewhere: an empty value, a quoted
value and an unquoted value with a trailing comment parse identically in
both.

*Verified* with one fixture driven through both branches, the ImportError
half forced by patching `sys.meta_path` rather than by hoping the package is
absent — a test that is green exactly where it proves nothing is worse than
no test.

### Corrected — the profile lifetime figure was inherited, not measured

The family template said a Scraping Browser profile's credentials "live
about a day" and derived from that the rule never to paste a working
endpoint into a README or a workflow. The figure carried no date, no method
and no citation, and it had propagated into a shipped file here:
`tools/verify_browser_api.sh` told users a 401 meant a day-old endpoint.

The vendor's Browser API documentation, read 2026-09-17, says:

> Profiles are stored for 90 days from creation, or until deleted by the
> user.

and the credentials do not expire on a timer; they are regenerated manually.
Measured against the live API the same day: a profile record carries
`createdAt` and `deletedAt` and **no expiry field**, and the connection
string came back byte-identical across three consecutive fetches, so it is
not reissued per request either.

**The rule was right and its reasoning was backwards.** An endpoint in a
public file is not a line that goes stale in a day — it is a live credential
that stays live, which is a stronger reason to keep it out. The shipped
script now says a 401 is a malformed or superseded login and points at the
client that fetches a fresh one.

### Measured, and NOT concluded

- **Applying a voucher by hand is still unsolved.** 2Captcha's documentation
  does not say how the solution reaches the site, and the how-to says to
  read the target's own code. AWS documents the token as the `aws-waf-token`
  cookie, also readable from an `x-aws-waf-token` header. A voucher placed
  in that cookie from a plain HTTP client did not clear the page. This does
  not matter on the primary path, where the browser's own extension does the
  applying — but it is why the fallback's own end-to-end path remains
  unproven.

- **A session segment does not reliably pin one exit address.** Ten sessions
  gave ten distinct addresses, which is what the generated pool rests on and
  it holds. But ONE session checked eight times in a row answered from two
  different addresses. The `sessTime-10` window is minutes — the same
  segment held at +2 minutes and had moved by +12 — yet it is not absolute
  within the window either. Any claim that "a worker owns one exit for its
  lifetime" is therefore too strong, and two WAF experiments were void
  before this was noticed, because they had treated a session label as an
  address.

### Measured

| Path | Result | When |
|---|---|---|
| Browser API, `--cdp-endpoint` | 25 rows, `status=complete`, 25/25 priced, `setAutoSolve enabled` | 2026-09-17 |
| 2Captcha proxy product, `--proxy-file` over 55 session-pinned exits | 25 rows, 100% price coverage | 2026-09-17 |
| `smoke_test.py`, Playwright installed | 509 checks | 2026-09-17 |

### Still open

**The AWS WAF solve has still never run against a live challenge.** No
challenge has appeared to solve, so the end-to-end path remains unexercised.
That is "not yet exercised" — not a verdict on the solver.

The challenge tally by position, each row tied to the run that produced it
rather than summed into one figure:

| Exit | Requests | Challenged | When |
|---|---|---|---|
| Maintainer's home connection | 10 | 0 | 2026-09-16 |
| Maintainer's home connection | 10 | 0 | 2026-09-17 |
| Proxied exits `us` / `de` / `in` / `br` | 5 | 0 | 2026-09-16 |
| Proxy pool (55 session-pinned exits) | one 25-row run | 0 | 2026-09-17 |
| Browser API exit | one 25-row run | 0 | 2026-09-17 |

One of the five proxied requests ran with TLS impersonation off. A
**datacentre** address — the one class of exit the 8-of-10 figure in this
repo's history came from — has still not been retried; whether the account
offers that type has not been established.

## [1.0.0] — 2026-09-16

First release in which **every engine, and the container, have been run
against the live site and compared row for row**. The version number is that,
not a feature list: what changed most between 0.4.1 and 1.0.0 is how much of
this repo has actually been executed.

Two of the three browser engines had never completed a single live fetch of
this site. Both were broken, in different ways, and neither failure was
visible to a green offline suite.

### Fixed

- **`selenium_scraper.py` could not load this site at all.** Selenium's
  default page-load strategy is `normal`, which blocks until the `load`
  event; www.transfermarkt.com never fires one that Chrome is willing to
  wait out. Measured 2026-09-16: every attempt died at exactly 60s with
  `timeout: Timed out receiving message from renderer`, three attempts per
  page, while `https://example.com` returned through the same driver in 0.2s
  and the other two engines had the same Transfermarkt page in under two
  seconds. Now set to `eager`, Selenium's equivalent of the
  `domcontentloaded` the other two engines already navigate with. A listing
  page's rows are in the first response here, so nothing is lost by not
  waiting for the last tracker pixel.
- **`puppeteer_scraper.py` could not launch a browser on Apple silicon.**
  pyppeteer downloads its own Chromium, and that build (revision 1181205) is
  **x86_64 only**; under Rosetta on this machine it printed its DevTools URL
  and then died with `mach_port_rendezvous ... Unknown service name`, which
  pyppeteer reports as the unhelpful `Browser closed unexpectedly`. pyppeteer
  reads no environment variable for an alternative binary, so there was no
  way around it from outside the code. Added **`--chromium-path`**, which
  passes `executablePath` through to the launcher; point it at any arm64
  Chromium and the engine works.

### Added

- **AWS WAF parity across all three engines.** Only `playwright_scraper.py`
  ever handled the challenge this site actually serves. The other two
  detected it through the generic markers and then had nothing to do about
  it: they never imported `detect_aws_waf`, and their only way to apply a
  solved token was to fill a `g-recaptcha-response` field that an AWS WAF
  page does not contain. Both now take the same branch Playwright does —
  detect first, then install the token as the `aws-waf-token` COOKIE scoped
  to the page's own host, then reload so the WAF re-checks it.
- **A parity test that drives all three real handlers**, not just their
  source, against the challenge fixture with the solver stubbed: each must
  report the challenge solved, set exactly one correctly-named, correctly
  scoped cookie holding the token verbatim, reload, and never reach for a
  reCAPTCHA field. Run in three separate virtualenvs, one per engine.
- **`--chromium-path` on the pyppeteer engine** (see above).

### Changed

- **The Scraping Browser API is now the documented default path** (audit
  B1). The README leads with `--cdp-endpoint`, the captcha solver is named
  as the fallback behind it, and the product table is ordered the way the
  products should be reached for. The endpoint's SHAPE is documented, never
  a live one.
- **The readiness wait no longer evaluates a string** (audit G1). All three
  engines call `page_flow.wait_for_count()`, a port of the same function in
  catawiki-scraper: it polls `querySelectorAll` through the protocol and
  returns the last count it saw, so a caller can tell "painted" from "timed
  out holding two rows". `page_flow.ready_count()` converts `min_matches`'s
  "strictly more than" floor into the "at least this many" the wait takes,
  in one place rather than a `+ 1` in each engine. A new offline group
  covers the arithmetic and scans every shipped file for the eval form.
- **The canary SKIPs instead of failing when it cannot pass** (audit E1).
  With `TRANSFERMARKT_CDP_ENDPOINT` or `TRANSFERMARKT_PROXY` configured the
  live run goes through it — via step `env`, never argv — and a block is a
  real failure. With neither, a block from a bare runner is a fact about
  that runner's address, so the job emits a `::notice::` and goes green.
  Every other exit code still turns the check red in every configuration, so
  a markup change cannot hide behind the skip.
- The canary's header no longer claims the site challenges nobody, and the
  README's troubleshooting entry for exit 3 no longer says datacentre
  addresses have not been established as an issue here. Both predated the
  v0.4.1 correction.

### Measured for this release, all on 2026-09-16

| What | Result |
|---|---|
| `tools/waf_probe.sh 10 market-values`, home connection, 17:17Z | 0 of 10 challenged, every response `http=200 server=nginx` |
| Playwright, live, `--mode market-values --pages 1` | 25 rows, price coverage 25/25, `status=complete` |
| Selenium, live, same command | 25 rows, 25/25, `complete` |
| pyppeteer, live, same command (`--chromium-path`) | 25 rows, 25/25, `complete` |
| Docker image, live, same command | 25 rows, 25/25, `complete` |
| `diff_runs.py` between all four | **0 added, 0 removed, 0 changed** in every pairing |
| Playwright, `--mode transfers --pages 2` / `--mode player --player-id 418560` | 50 rows / 1 row, both `complete` |
| `smoke_test.py` | 459 checks with no engine installed; 494 with Playwright, 481 with Selenium, 483 with pyppeteer |

**The Docker image was built and run for the first time in this repo's
history.** `docker build` succeeds; the entrypoint answers `--help`; Chromium
really launches inside it (153.0.8010.12, not just an `apt-get` that exited
0); the image contains no `.env`, no test suite and no fixtures; and a
container scraped the live site and produced rows identical to every other
engine.

### Still open

- **The AWS WAF solve has never run against a live challenge.** The task
  schema is verified against the API with a control and all three engines now
  implement the path, but no challenge has been met to solve — six network
  positions drew none on 2026-09-16. This is "not yet exercised", not a
  verdict on the solver.
- **The canary's SKIP branch has not been dispatched on GitHub.** It was
  verified by running the step's own shell across all seven exit codes and
  both secret configurations. Dispatch it once after this lands.
- **The Scraping Browser zone on the development account refuses CONNECT
  selectively by host**, so `--cdp-endpoint` reaches `example.com` but not
  `www.transfermarkt.com`. That is an account/zone matter, documented in the
  README's troubleshooting section, not a scraper bug.

## [0.4.1] — 2026-09-16

> **Correction to v0.4.0 and earlier.** Those releases stated that this site
> needs no captcha, no proxy and no browser — in the README, in
> `.env.example`, in `scraper_api_client.py`'s docstring and in the canary's
> own header comment. **That was wrong**, and wrong in the direction that
> costs a reader money: it told anyone hitting a challenge that a 2Captcha
> key would not help here.
>
> Transfermarkt sits behind **AWS WAF on CloudFront**. Measured 2026-09-16 —
> ten consecutive requests to the market-values ranking, one datacentre
> address, one user agent, two seconds apart — **eight were answered with an
> AWS WAF CAPTCHA** (HTTP 405, `x-amzn-waf-action: captcha`, `server:
> CloudFront`, a 2331-byte "Human Verification" body) and two with the real
> page (`server: nginx`). 2Captcha solves that captcha type, and has for
> years; this repo simply had not implemented it.
>
> The released v0.4.0 section below is left exactly as it shipped. It is
> history, and `git show v0.4.0:CHANGELOG.md` will show the same text.

The measurement that produced the old claim was taken through a proxied
fetch and written up as a property of the site. It is a property of an
address on a day: from a 2Captcha proxy exit on the same date the real page
came back with zero solves and zero renders. Neither figure travels without
its network position.

### Fixed

- **The only challenge this site serves was invisible to the detector, and
  every engine reported it as an empty listing.** `BOT_CHALLENGE_MARKERS`
  covered six vendors — Cloudflare, DataDome, PerimeterX/HUMAN, reCAPTCHA,
  hCaptcha and a generic set — none of which appears on this site, and
  omitted AWS WAF, which is the one that does. Worse than a miss: with no
  marker matching and no content hooks present, a 2331-byte challenge fell
  through to `empty`, whose policy is `retry: False, solve: False,
  blocked: False` — so a captcha was reported as a genuinely empty listing
  (exit 4, "zero rows"), which is not even counted as a failure. Not
  hypothetical and not limited to one engine: `page.goto()`'s return value
  was discarded in Playwright and never captured at all in Selenium or
  pyppeteer, so **all three browser engines classified the challenge as
  `empty`**, at HTTP 405, with no configuration change required.
- **The solver could never have been offered this captcha even once it was
  detected.** A non-2xx status returned `blocked` before anything else ran,
  and `STATE_POLICY["blocked"]` carries `solve: False` — so a run rotated
  exits, spent its retry budget and exited 3 without ever attempting the
  solve. The status check now runs LAST among the positive tests, after the
  marker scan, so a 405 that is really a captcha is classified as one.
- **`x-amzn-waf-action` was received and thrown away.** The Scraper API
  returns `{"status", "headers", "body"}`; `fetch_html` kept the first two
  and dropped the headers one line before the classifier that needed them.
  The header is now consulted before anything else, being the front naming
  its own action.
- **The classification order let a marker outrank the site's own content.**
  The vendor scan ran on every page, including good ones, so any marker that
  happens to appear on a served page turned a healthy run into exit 3 — the
  failure a sibling repo had twice in two releases. The positive content
  signal now runs first, and the marker scan only refines a page that has
  already failed to look like content.
- **Two engines told the reader a captcha was unsolvable** when the key was
  absent, rather than naming the key as the thing that was missing. Both now
  say what to set, and the old phrasing is in the suite's banned-phrase list
  so it cannot come back. (This entry deliberately does not quote the old
  sentence: the guard is absolute and has no per-file exemption, which is
  the point of it.)

### Added

- **AWS WAF solving**, task type `AmazonTaskProxyless` — field names taken
  from 2Captcha's own API documentation rather than from this client's
  existing field names. `captcha_solver.detect_aws_waf` reads `websiteKey`,
  `iv` and `context` out of the `window.gokuProps` the challenge page
  publishes, plus both optional script URLs; no runtime interception is
  needed, unlike the Cloudflare Turnstile case. An incomplete detection
  refuses to build a task rather than buying a rejection at full price.
  The answer is set as the `aws-waf-token` **cookie**, not into a form
  field, so the injection path is separate from reCAPTCHA's — wired in
  `playwright_scraper.py`.

  Verified against the **live** 2Captcha API with a real key on 2026-09-16,
  with a control: an invalid task type is rejected (`ERROR_TASK_ABSENT`),
  while `AmazonTaskProxyless` is accepted and names its own missing field
  (`ERROR_PAGEURL`) — so the type and the field names are right. What is
  still NOT verified is a solve against a live challenge, because that needs
  a network position that actually gets challenged.

  That check also turned up something worth knowing before anyone relies on
  this path: **`createTask` validates almost nothing.** A task built with a
  fabricated `websiteKey` and no `iv`/`context` was accepted, returned
  `status: ready`, and was charged $0.00145. An incomplete detection
  therefore does not fail — it silently buys a token that cannot clear the
  challenge. `detect_aws_waf` refusing to build a task from a partial read
  is what prevents that, and it is now justified by measurement rather than
  by principle.

  The solution also distinguishes `captcha_voucher` (a challenge really was
  solved) from `existing_token` (2Captcha's own exit was not challenged, so
  this is just the site's ordinary token). The solver now logs the second
  case explicitly, because it means the exit — not the solver — is the
  variable.
- 26 offline checks covering the above (395 → **421** with no engine library
  installed, 448 → **474** with all three), including §18's control that
  every new marker counts zero on five real captured pages and non-zero on
  the challenge, and that a served page carrying 2Captcha's own injected WAF
  hunter still classifies as content.

### Verified live, with a real key (2026-09-16)

Run end to end against the real services, not against fixtures. The site
itself is unreachable from the environment this was done in (its egress
policy denies the host), so everything below goes through 2Captcha's own
API, which is what the browserless engine talks to anyway.

- **`scraper_api_client.py`, all four modes**: market-values (25 rows,
  price coverage 25/25), transfers (25 rows, all five `fee_type` values
  represented), club-squad (25 rows), player (1 row). Every run exit 0,
  `status=complete`. This exercises the new three-value `fetch_html` and
  the header plumbing added above.
- **Multi-locale, live**: the same run against `transfermarkt.de` and
  `transfermarkt.jp` returned 25 rows each with positions and nationalities
  normalised to English (0 untranslated), and `source` carrying the real
  host. The 25 players shared with the `.com` run had **identical prices in
  all 25 cases**, which is the locale-aware money parsing checked against
  live data rather than against a saved page.
- **Player detail, live**: dates normalised to ISO (`2000-07-21`,
  `2022-07-01`, `2034-06-30`, `2026-07-22`), `full_name` read from the
  info-table, height and agent populated.
- **`fingerprint_client.py`**: exit 0 against the live API, key read from
  `.env` through `env_config`. Applying that real fingerprint produces a
  context carrying its own user agent, its own locale (`en-US`, not a
  synthesised `en-{country}`), its timezone and its `deviceScaleFactor` —
  the four defects this family found in sibling repos are all absent here,
  now checked against real API output rather than a fixture.
- **`captcha_solver.get_balance`**: live, returns a balance.
- Still NOT verified: a solve against a real AWS WAF challenge, which needs
  an exit that actually gets challenged; and the Docker image, which needs
  a daemon.

### Fixed — found by running the above

Two checks in the offline suite asserted things about the *developer's
machine* rather than about the repo, and both failed the moment a real
`.env` existed. The README tells a new user to create one and then to run
the suite, so the suite was red for anyone who followed the instructions.

- **"no .env file is committed" tested for the file's EXISTENCE.** The
  invariant is that one is never committed, not that one never exists. Now
  asserts that `.gitignore` excludes `.env` and that git does not track it,
  which is the actual property, and which also works in an export with no
  `.git` directory.
- **The "no key anywhere" assertion could not isolate itself from a real
  `.env`.** It cleared the environment variable but `env_config.load_env()`
  defaults to the `.env` next to the scripts, so the key came back and
  `main()` succeeded where the test expected a refusal. Now points the
  loader at a path that does not exist, which leaves the precedence logic
  under test instead of stubbing it out, and touches nothing on disk.

### Fixed — Browser API parity

- **pyppeteer connected over CDP but never enabled the Scraping Browser's
  own auto-solve.** `playwright_scraper._connect_remote` sends
  `Captcha.setAutoSolve` and listens for `Captcha.detected` /
  `solveFinished` / `solveFailed`; the pyppeteer engine did the
  `browserWSEndpoint` connect and then nothing. A run with `--cdp-endpoint`
  therefore got auto-solve on one engine and silently not on its twin —
  engine drift (§6) on the paid path. Now identical in both, with the same
  graceful log when an endpoint does not carry the Captcha domain. Selenium
  stays out on purpose, not by oversight: `debuggerAddress` takes a bare
  host:port with nowhere for a password, so it cannot reach an
  authenticated endpoint at all. Pinned by a source assertion per engine so
  it cannot drift back.

### Known, and not fixed here

- The canary still runs unconditionally on a bare GitHub runner, which is a
  datacentre address — the network position measured at 8/10 challenged. It
  should gate on a proxy/CDP secret and skip green like `mediamarkt-scraper`
  does, or run through the Scraping Browser API.
- Selenium and pyppeteer detect the challenge by marker but do not yet set
  the `aws-waf-token` cookie, so they report it rather than clearing it.
- The readiness wait still hands the browser an evaluated string
  (`wait_for_function`), which a CSP without `unsafe-eval` refuses outright.
  `catawiki-scraper`'s `page_flow.wait_for_count()` is the known-good fix.

## [0.4.0] — 2026-09-16

Multi-locale support: four more Transfermarkt hosts verified and added
(`www.transfermarkt.de`, `.world` [Russian], `.co.kr` [Korean], `.jp`
[Japanese]), alongside the pre-existing English `.com`. Every one of the
five was fetched live before being added — a market-values ranking page in
each, a full club-squad page in German, and the same real player's profile
(Erling Haaland, id 418560) in all four new locales — and diffed against
the equivalent English capture field-by-field, per this repo's own "measure
before claiming" rule and CLAUDE.md's mediamarkt.lu lesson about same-brand
hosts.

Structurally the five hosts are identical: same `table.items`/`posrela`/
`hauptlink`/`flaggenrahmen` classes, same `/profil/spieler/{id}` and
`/verein/{id}` URL/id scheme (the slug segment is never transliterated,
even into Cyrillic/Hangul/Kanji). So no row-extraction selector changed —
only the TEXT those selectors read, which is normalised into a single
English vocabulary as requested, rather than shipping five parallel sets of
position/nationality/date strings for one underlying fact.

### Added

- **`LOCALE_BY_HOST`** — the host->locale map backing the four new hosts;
  `HOSTS`/`unsupported_reason` now accept exactly these five and continue
  to refuse every other Transfermarkt TLD (`.es`, `.co.uk`, ...) with a
  reason, since those were not fetched or checked.
- **`parse_market_value(text, locale=...)`** is now locale-aware: decimal
  separator, symbol position (prefix "€220.00m" in English vs. suffix
  "220,00 Mio. €" everywhere else) and suffix vocabulary
  (Mio./млн/mil./m for "million") all come from `_MONEY_FORMATS`, measured
  on a live ranking-page fetch per locale. Every value is still EUR in
  every locale — Transfermarkt does not convert, only reformats.
- **`translate_position` / `translate_nationality`** — per-locale
  {native: English} lookup tables built by matching the SAME real player's
  row across a locale capture and the English capture at the identical DOM
  position (a market-values top-25 shared across all five locales, plus a
  full Manchester City squad page shared between en/de for the fuller
  German table). Coverage is intentionally uneven (German has 10 positions/
  14 nationalities from the richer squad-page capture; ru/ko/ja have 7-8
  each from the market-values page) — an untranslated string passes through
  unchanged with a logged warning rather than a guessed translation.
  Japanese nationality text is left unchanged entirely: measured to already
  be in English on that locale (flag `title`/`alt` text), unlike JP club
  names elsewhere on the same page, which ARE localised — a real site
  asymmetry, not a scraping gap.
- **`normalize_date(text, locale=...)`** — the five locales' date shapes
  (measured on the same player's profile in every locale) reduced to ISO
  8601 (`YYYY-MM-DD`). Not just a separator difference: en/de/ru are
  day-first, ko/ja are year-first, and Russian spells the month out in
  genitive case (`"21 июля 2000 г."`) rather than using digits — a
  hardcoded 12-entry table of that fixed vocabulary, not sampled data.
  Applied to `birth_date`, `joined_date`, `contract_until` and
  `market_value_last_update`.
- **`_HEADER_LABEL_KEYS` / `_INFO_LABEL_KEYS` / `_LAST_UPDATE_PREFIX`** —
  the German/Russian/Korean/Japanese label text for the player-detail
  fields that were previously looked up by hardcoded English key only
  (`position`, `joined`, `contract expires`, `name in home country`,
  `place of birth`, the market-value "Last update:" prefix), each measured
  on Erling Haaland's own profile fetched in every locale. The "full name"
  row's non-English label was deliberately NOT added: no non-English
  capture available exercises that row (every one on hand shows "name in
  home country" instead) — see `_INFO_LABEL_KEYS`'s comment for why
  guessing it by analogy would have been exactly the kind of fabrication
  this project avoids.
- Locale-aware smoke tests (`test_locale_money_parsing`,
  `test_locale_translation`, `test_normalize_date`,
  `test_locale_market_values`, `test_locale_player_detail`), the latter two
  against two new real-capture fixtures (`FIX_MARKET_VALUES_DE`,
  `FIX_PLAYER_DETAIL_DE`) trimmed from live `www.transfermarkt.de` fetches
  the same way the existing English fixtures were. `smoke_test.py`: 330 ->
  395 checks with no engine library installed, 383 -> 448 with all three.
- A "Locales" section in README.md documenting what's supported, what's
  translated and how, and the known gaps (uneven per-locale coverage,
  transfers-listing fee-type text is English-only, club names are not
  translated).

### Fixed

- **Every row-constructing function (`parse_market_values`,
  `parse_club_squad`, `parse_transfers`, `parse_player_detail`) hardcoded
  `source="transfermarkt.com"`** regardless of which host was actually
  fetched — a `.de` fetch would have reported English provenance on every
  row. Now derived from `site_host(base_url)`, the same pattern
  `scraper_api_client.finish_run` already used for its own `source` field.

## [0.3.0] — 2026-09-12

A full parity pass against this family's other four members (catawiki-,
amazon-, mediamarkt- and etsy-scraper) — code AND infrastructure — plus a
fresh live capture used to re-verify the parser rather than trusting the
2026-09-10 fixtures alone. Four real bugs fixed, one already-defined function
that nothing called, comprehensive logging where `product_parser.py` had
none at all, and the CI/CD scaffolding this file's own 0.1.0 entry and the
README already described as shipped but the repo did not actually contain.

### Fixed

- **`scraper_api_client.py` treated `state=blocked` as final on the FIRST
  attempt, ignoring `--retries` entirely.** Every other outcome in the same
  function (a vendor challenge page) already retried up to the configured
  budget before giving up; a transient upstream error or empty body landed
  in the same `blocked` classification and skipped that budget completely.
  This was the one place in the whole family where `--retries` silently did
  nothing for that outcome. Fixed by retrying it the same way.
- **`fingerprint_client.py` was the one CLI in this repo that read only its
  own `--key` flag**, never `.env` or an exported `TWOCAPTCHA_KEY` the way
  every other entry point here does via `env_config.apply()`. The first
  thing this project's own troubleshooting guidance tells someone to run
  when a key "isn't working" (`python3 fingerprint_client.py`) could not see
  a key set the normal way.
- **`playwright_context_kwargs()` never carried a fingerprint's own
  `deviceScaleFactor`/`devicePixelRatio`**, so a Retina/HiDPI fingerprint
  still rendered through a plain 1x context.
- **`proxy_pool.ProxyPool.rotates_per_page()` was defined and unit-tested
  but called from nowhere** — `--proxy-rotate per-page` was accepted,
  parsed, and did nothing in all three browser engines. Wired into each
  engine's sequential-pagination loop.
- **`scraper_api_client.py` was the one engine that never logged a
  dropped-duplicate-row count on merge**, the line every browser engine in
  this family already prints — a run that silently re-fetched the same page
  looked identical to one that made real progress.

### Added

- Logging throughout `product_parser.py`, which had none anywhere before
  this pass: a `logger.warning` when a table/header this repo expects is
  simply absent, and a skipped/total summary per parse.
- `page_flow.is_thin_page()` / `THIN_PAGE_RATIO` — an informational-only
  diagnostic (deliberately not a hard page cap; the sku-based
  `no_new_products` stop already covers real exhaustion) logged by all four
  engines when a page returns well under page 1's row count.
- `page_flow.PRICE_COVERAGE_FLOOR`, enforced (as a warning, not a hard stop)
  in all four engines for `--mode market-values`/`club-squad`.
- `diff_runs.TRACKED_FIELDS` widened from 2 generic fields (`price`,
  `currency`) to 13, adding 11: `club`/`club_id`/`position`/`age`/
  `nationality`/`market_value_last_update` (Player-mode) and `from_club`/
  `from_club_id`/`to_club`/`to_club_id`/`fee_type` (Transfer-mode). It
  previously tracked nothing specific to a `transfers`-mode run at all (a
  re-loan to a different club under the same fee type went unreported), and
  missed every Player-mode field except the two generic ones too.
- `.env.example`, matching `env_config.ENV_KEYS` exactly.
- The `.github/` scaffolding this file's own 0.1.0 entry and the README
  already claimed ("CI: `tests.yml` ... and `canary.yml`") but the repo did
  not actually ship: `workflows/tests.yml`, `workflows/canary.yml`,
  `workflows/claude.yml`, `workflows/claude-code-review.yml`,
  `ISSUE_TEMPLATE/*.yml`, `ci_checks.py`, `.gitignore`, `.dockerignore`.
  `canary.yml` is not a copy of a sibling's — it runs unconditionally, no
  proxy secret required, because this site (unlike this family's
  e-commerce members) was measured to need none; see its own header for the
  full reasoning.
- New `smoke_test.py` checks covering every fix above, plus a check that
  `.github/ci_checks.py` is actually invoked by `tests.yml` rather than
  merely present — the exact "shipped but wired to nothing" shape several
  of the fixes above already took. Total count: **330 with no engine
  library installed** (up from 320), **383 with all three** (up from 340) —
  most of the growth is engine-gated (the `rotates_per_page()`/
  `is_thin_page()`/`PRICE_COVERAGE_FLOOR`/dropped-duplicate wiring checks,
  one set per browser engine).

### Verified live

- Re-fetched `market-values` (2 pages), `club-squad` (Manchester City),
  `transfers` (2 pages, both the plain `?page=N` shape and a `rel=next`
  shape), and a player profile on 2026-09-12 and ran this repo's own parser
  against the fresh HTML: 25/25 rows with 100% field coverage on
  `market-values` on both pages measured a page apart; 43 rows on
  `club-squad`, 36/43 priced (unpriced rows are real — youth/loan players
  the site itself shows with no market value); 25 rows on `transfers`, only
  3/25 carrying a fee (expected: most rows in this window were loans or
  free transfers, not the parser dropping the fee).
- One club-squad row set initially looked wrong (players from other clubs
  appearing on Manchester City's squad page) and was confirmed, via the
  page's own title and canonical URL, to be the site's real behaviour for a
  loaned-out player rather than a parser bug — no code changed for it.

## [0.2.0] — 2026-09-10

A v0.1 review pass: two real parsing bugs found by running the parser
against freshly fetched live pages (not just the fixtures), one exit-code
gap against the family's own contract, and one architectural claim
("`market-values` pagination can never be addressed statelessly") that
turned out to be true only 1 time in ~29, not always. Nothing here is a
behaviour regression for existing output columns except `full_name`, which
is fixed below and was wrong before.

### Fixed

- **`full_name` silently duplicated the display name (`title`) on every
  `--mode player` row instead of reading the site's real full name.** The
  site publishes it in a SEPARATE "Facts and data" info-table panel the
  parser never read — under a "Full name" row on most profiles, or "Name in
  home country" on ones where the two differ (Erling Haaland's real name is
  "Erling Braut Håland"; confirmed live on both). This was a regression from
  the pre-rewrite single-file scraper, which read this exact box generically.
  `full_name` is now `None` rather than a copy of `title` when neither row
  is present, per this family's "never present a guess as a fact" rule.
  See `product_parser._info_table_map`.
- **`birth_place` could ship the site's own CSS-truncated text (`"Esplugues
  de ..."`) instead of the real value.** The header's copy of this field
  sits in a `span.cp` the site visually clips with an ellipsis, keeping the
  full string only in a `title` attribute — the same class of bug
  `_detitle` already guarded against for a doubled link title, just
  triggered by truncation instead. Confirmed live on Lamine Yamal's profile
  (real value "Esplugues de Llobregat"). Fixed two ways: the info-table
  panel's own copy of this field is never truncated and is now the primary
  source, with `_text_or_title` as a defensive fallback for a profile
  missing that panel.
- **Exit code `5` ("remote API error" in this family's own contract,
  CLAUDE.md §9) was never used.** A Fingerprint API failure or a
  `--cdp-endpoint` connect failure raised a bare `RuntimeError` no engine
  caught, so either one reached the interpreter as an unhandled exception
  and exited `1` — a raw traceback, no run-metadata sidecar — regardless of
  which it actually was. Added `output_writer.RemoteAPIError` /
  `EXIT_REMOTE_API_ERROR`, raised from `fingerprint_client.get_fingerprint`
  and each engine's `--cdp-endpoint` connect path, caught once per engine's
  entry point. Deliberately NOT what a captcha-solve failure gets — that
  stays a warning that lets the run continue, per this file's own captcha
  section.
- **The gallery pagination trap was real but was documented as the
  deterministic shape of `market-values` pagination, when it is a rare,
  intermittent one.** The original 2026-09-10 capture (a doubled
  `galerie/{uuid}/page/N/page/M` next-link whose stateless page-2 fetch
  replays page 1) was genuine, but a wider re-measurement the same day found
  the plain `?page=N` shape — identical to `transfers`, and genuinely
  independently addressable (0 shared player ids between pages) — on 28 of
  29 consecutive fresh fetches of the same URL. `page_flow.
  pagination_is_addressable()` hardcoded `False` for this mode
  unconditionally instead of running the same "does page 1's own next-link
  agree with the constructed URL" check `transfers` already used. Fixed by
  removing the special case: market-values now gets the plain-URL fast path
  in the common case, and still correctly falls back to chaining the site's
  own link one page at a time in the rare case — the fallback itself did
  not change, only when it engages. `--concurrency` remains
  `transfers`-only, conservatively, pending a live run that tests several
  workers against a mode that can rarely switch shape mid-run.

### Added

- **`scraper_api_client.py`** — the fourth, browserless engine this
  family's other members ship, ported from `mediamarkt-scraper` (site
  knowledge there is `~none`, per CLAUDE.md's own module table) rather than
  written on spec. v0.1 shipped without it reasoning that porting a fourth
  engine with no measured page needing it would be dead code; that
  reasoning did not hold once a plain, unauthenticated fetch was confirmed
  to return complete, current HTML for all four page kinds with no browser,
  no proxy and no challenge markup. Paginates `--mode transfers` fully and
  `--mode market-values` whenever page 1 says it can (see the pagination
  fix above); reports `partial` (exit 6) rather than a silent short result
  on the rare run it cannot.

## [0.1.0] — 2026-09-10

Initial release. Rebuilt from an earlier single-file, ~24-category
`transfermarkt.com` scraper onto this org's shared family architecture (the
same core-module split as `farfetch-scraper`, `amazon-scraper` and
`mediamarkt-scraper`), scoped to four modes for v0.1: **market-values**,
**club-squad**, **transfers**, and **player** detail.

### Added

- `product_parser.py` — DOM-anchored extraction for all four modes. This
  site publishes no JSON-LD anywhere (checked on every page kind covered
  here), so unlike this family's e-commerce members there is no
  structured-data primary path; every field is read off URL patterns,
  `schema.org/Person` microdata, or the site's own class names.
- `output_writer.py` — two row schemas, `Player` and `Transfer`, rather than
  one. See its docstring for why folding both into a single dataclass would
  produce permanently-null columns on every run of one mode or the other.
- `page_flow.py` — the shared page-state policy, including
  `pagination_is_addressable()`, which returns `False` unconditionally for
  `market-values` (see "the gallery pagination trap" below) and checks
  agreement between the site's own next-link and a constructed URL for
  `transfers`.
- Three engines: `playwright_scraper.py` (primary, the only one with
  `--concurrency`), `selenium_scraper.py`, `puppeteer_scraper.py` (via
  pyppeteer — a Python port of Puppeteer's API, not a Node.js process).
- `diff_runs.py`, `env_config.py`, `proxy_pool.py`, `fingerprint_client.py`,
  `captcha_solver.py` — carried over from this family's shared core, edited
  only where this site's own behaviour differs (see below).
- `smoke_test.py` — 259 offline checks with no engine installed, 300 with
  all three, against real captured HTML, covering
  all four parsers, both dataclasses, the writers, the diff tool, the
  captcha/bot-detection wiring, credential handling, and an AST-based
  undefined-name and Dockerfile-completeness check for every shipped module.
- CI: `tests.yml` (offline, Python 3.9/3.12 matrix, one-venv-per-engine
  `engine-smoke`, a Docker build-and-run job) and `canary.yml` (a real daily
  run against the `transfers` listing).
- Docker image built around the Playwright engine.

### Discovered and fixed during development

- **The gallery pagination trap.** `market-values`'s own `<link rel="next">`
  carries a doubled, 404ing path segment, and even the hand-corrected URL
  replays page 1's content when fetched statelessly — the `galerie/{uuid}`
  segment is server-side search/session state, not a page-number parameter.
  `pagination_is_addressable()` refuses to construct a page-2+ URL for this
  mode; the engines chain the site's own link one page at a time inside a
  single browser session instead.
- **`joined_date`/`contract_until` came back `None` on every player
  profile**, despite being visible on the page. Cause: the profile header
  uses `data-header__label` on both `<li>` (birth date/position/agent) and
  `<span>` (Joined/Contract expires) tags — the same class on two different
  elements. Fixed by broadening `_label_map`'s selector to match both.
- **A transfer's `from_club` read as `"Without ClubWithout Club"`** for the
  "Without Club" free-agent placeholder. Two compounding causes: the site's
  own markup genuinely carries a doubled `title` attribute for this
  placeholder, AND the cell holds two `<a>` tags for the same club — an
  image-only wrapper with empty text, and the real text-bearing anchor.
  Selecting the first matching link picked the image wrapper, so the
  doubled-title guard (`_detitle`) never had real text to fall back to.
  Fixed by selecting `td.hauptlink a` specifically.

### Known limitations

- This repo's own build/test environment could not complete a live browser
  connection to `www.transfermarkt.com` — traced to the build sandbox's own
  outbound allowlist, not to the site. Whether a live run needs a proxy at
  all, and whether datacentre addresses are treated any differently here,
  is genuinely unmeasured. See README's "Known limitations" section.
- Only `www.transfermarkt.com` is supported; other locale sites were not
  checked.
- A browserless HTTP client (this family's usual fourth "engine") was not
  ported for v0.1 — every page kind needed a real browser navigation to
  capture during development, so porting one on spec would be dead code.
- Three modes plus player detail, not the previous single-file scraper's
  ~24 categories — a deliberate v0.1 scope decision.
